"""Git transport for offline Codex snapshots; archives, not SQLite git merges."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import uuid
import zipfile

import codex_chat_merge as core

REPO_FORMAT = "codex-chat-git-v1"
ARCHIVE_FORMAT = "codex-chat-archive-v1"
MARKER = ".codex-chat-repo.json"
PART_SIZE = 48 * 1024 * 1024
ITERATIONS = 600_000


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise core.MergeError("设备和快照标识只能包含英文字母、数字、下划线和短横线，最长 80 字符")
    return value


def settings_path():
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "CodexChatMerge" / "settings.json"


def load_settings(path=None):
    path = Path(path or settings_path())
    existing = core.read_json(path) if path.exists() else {}
    owner = existing.get("owner_id") or str(uuid.uuid4())
    hostname = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname()).strip("-_")[:32] or "pc"
    defaults = {"owner_id": owner, "device": hostname + "-" + owner[:8],
                "url": "", "repo": str(Path.home() / "CodexChatBackups" / "git-repo"),
                "encrypt": True}
    return {k: existing.get(k, v) for k, v in defaults.items()}


def save_settings(settings, path=None):
    # Explicit allowlist: passwords and credential tokens never enter this file.
    core.write_json(path or settings_path(), {k: settings[k] for k in
                    ("owner_id", "device", "url", "repo", "encrypt")})


def validate_url(url):
    if not isinstance(url, str) or not url or url.startswith("-") or any(x in url for x in "\r\n\0"):
        raise core.MergeError("请输入有效的 Git 仓库地址")
    if "::" in url:
        raise core.MergeError("不支持 Git 外部传输助手地址")
    if url.startswith(("https://", "ssh://")):
        p = urllib.parse.urlsplit(url)
        if not p.hostname or p.password or p.query or p.fragment:
            raise core.MergeError("仓库地址不能包含密码、令牌、查询参数或片段")
        if p.scheme == "https" and p.username is not None:
            raise core.MergeError("HTTPS 地址请勿包含账号或令牌；使用 Git 凭据管理器登录")
        return url
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
        return url
    if Path(url).is_absolute():
        return str(Path(url).resolve())  # Also useful for LAN folders and offline tests.
    raise core.MergeError("支持 HTTPS、ssh://、git@主机:路径 或本地绝对仓库路径")


def aes_key(password, kdf):
    if kdf.get("name") != "PBKDF2-HMAC-SHA256" or kdf.get("iterations") != ITERATIONS:
        raise core.MergeError("不支持的加密参数")
    try:
        salt = bytes.fromhex(kdf["salt"])
        if len(salt) != 16:
            raise ValueError()
    except (KeyError, ValueError, TypeError) as e:
        raise core.MergeError("无效的加密参数") from e
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32)


def aes_class():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError as e:
        raise core.MergeError("加密备份需要 cryptography。请执行：python -m pip install cryptography") from e


def associated_data(header, number):
    fields = {k: header[k] for k in ("format", "device", "snapshot_id", "encrypted", "kdf")}
    fields["part"] = number
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")


def pack_snapshot(snapshot, output, device, password=None, *, part_size=PART_SIZE, log=print):
    device = identifier(device)
    snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
    core.disjoint(snapshot, output)
    if output.exists():
        raise core.MergeError("分卷输出目录已存在")
    if not 1024 <= part_size <= PART_SIZE:
        raise core.MergeError("分卷大小必须介于 1 KiB 与 48 MiB 之间")
    if password is not None and len(password) < 12:
        raise core.MergeError("加密密码至少 12 个字符；请在两台电脑使用相同密码")
    manifest = core.load_snapshot(snapshot)
    archive_id = identifier(core.stamp())
    header = {"format": ARCHIVE_FORMAT, "device": device, "snapshot_id": archive_id,
              "encrypted": password is not None, "kdf": None, "parts": []}
    cipher = None
    if password is not None:
        cls = aes_class()
        header["kdf"] = {"name": "PBKDF2-HMAC-SHA256", "iterations": ITERATIONS, "salt": os.urandom(16).hex()}
        cipher = cls(aes_key(password, header["kdf"]))
    output.mkdir(parents=True)
    log("压缩聊天快照并生成 Git 分卷……")
    try:
        with tempfile.TemporaryDirectory(prefix="codex-chat-pack-", dir=output.parent) as tmp:
            archive = Path(tmp) / "snapshot.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                for rel, expected in manifest["files"].items():
                    checksum, size = hashlib.sha256(), 0
                    with core.safe_path(snapshot, rel).open("rb") as src, z.open(rel, "w", force_zip64=True) as dst:
                        for chunk in iter(lambda: src.read(1024 * 1024), b""):
                            checksum.update(chunk)
                            size += len(chunk)
                            dst.write(chunk)
                    if size != expected["size"] or checksum.hexdigest() != expected["sha256"]:
                        raise core.MergeError("打包期间快照文件改变，请重新导出：" + rel)
            header["archive_size"] = archive.stat().st_size
            with archive.open("rb") as f:
                for number in range(1, 1_000_001):
                    chunk = f.read(part_size)
                    if not chunk:
                        break
                    part = {"name": f"part-{number:06d}.bin"}
                    if cipher is not None:
                        nonce = os.urandom(12)
                        chunk = cipher.encrypt(nonce, chunk, associated_data(header, number))
                        part["nonce"] = nonce.hex()
                    (output / part["name"]).write_bytes(chunk)
                    part.update(size=len(chunk), sha256=hashlib.sha256(chunk).hexdigest())
                    header["parts"].append(part)
                    log(f"已生成分卷 {number}，{'加密' if cipher is not None else '未加密'}")
            core.write_json(output / "snapshot.json", header)
            return header
    except BaseException:
        core.write_json(output / "FAILED.json", {"error": "分卷未完成，不能上传"})
        raise


def unpack_snapshot(directory, output, password=None, log=print):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    core.disjoint(directory, output)
    if output.exists():
        raise core.MergeError("下载解包目录必须是新目录")
    header = core.read_json(directory / "snapshot.json")
    if header.get("format") != ARCHIVE_FORMAT:
        raise core.MergeError("不支持的快照分卷格式")
    identifier(header["device"])
    identifier(header["snapshot_id"])
    parts = header.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 1_000_000:
        raise core.MergeError("无效的分卷清单")
    cipher = None
    if header.get("encrypted") is True:
        if password is None or not password:
            raise core.MergeError("该快照已加密，请填写备份密码")
        cipher = aes_class()(aes_key(password, header["kdf"]))
    elif header.get("encrypted") is not False or header.get("kdf") is not None:
        raise core.MergeError("无效的加密标记")
    output.parent.mkdir(parents=True, exist_ok=True)
    log("校验分卷并解包快照……")
    with tempfile.TemporaryDirectory(prefix="codex-chat-unpack-", dir=output.parent) as tmp:
        archive = Path(tmp) / "snapshot.zip"
        with archive.open("wb") as out:
            for number, part in enumerate(parts, 1):
                if part.get("name") != f"part-{number:06d}.bin":
                    raise core.MergeError("分卷名或顺序无效")
                p = directory / part["name"]
                if p.is_symlink() or not p.is_file() or p.stat().st_size != part.get("size") or p.stat().st_size > PART_SIZE + 16:
                    raise core.MergeError("分卷缺失、过大或不完整：" + part["name"])
                chunk = p.read_bytes()
                if hashlib.sha256(chunk).hexdigest() != part.get("sha256"):
                    raise core.MergeError("分卷校验失败：" + part["name"])
                if cipher is not None:
                    try:
                        nonce = bytes.fromhex(part["nonce"])
                        if len(nonce) != 12:
                            raise ValueError()
                        chunk = cipher.decrypt(nonce, chunk, associated_data(header, number))
                    except Exception as e:
                        raise core.MergeError("解密失败：密码错误或备份遭到修改") from e
                out.write(chunk)
        if archive.stat().st_size != header.get("archive_size"):
            raise core.MergeError("完整压缩包长度不符")
        extracted = Path(tmp) / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as z:
            names = set()
            # Validate the entire directory before writing a single archive member.
            for item in z.infolist():
                name = item.filename
                if name.casefold() in names or (name != "manifest.json" and not core.allowed(name)):
                    raise core.MergeError("压缩包含有重复或越界路径")
                if item.is_dir() or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                    raise core.MergeError("压缩包包含不支持的目录项或链接")
                names.add(name.casefold())
            if "manifest.json" not in names:
                raise core.MergeError("压缩包缺少 manifest.json")
            if z.getinfo("manifest.json").file_size > 20 * 1024 * 1024:
                raise core.MergeError("压缩包清单异常大")
            manifest = json.loads(z.read("manifest.json"))
            if set(names) != {x.casefold() for x in manifest["files"]} | {"manifest.json"}:
                raise core.MergeError("压缩包内容与文件清单不符")
            for item in z.infolist():
                if item.filename != "manifest.json" and item.file_size != manifest["files"][item.filename]["size"]:
                    raise core.MergeError("压缩包文件长度与清单不符")
            for item in z.infolist():
                dest = extracted / "manifest.json" if item.filename == "manifest.json" else core.safe_path(extracted, item.filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        core.load_snapshot(extracted)
        shutil.move(str(extracted), str(output))
    log("快照解包完成：" + str(output))
    return output


class GitStore:
    def __init__(self, url, repo, device, owner_id, log=print):
        self.url = validate_url(url.strip())
        self.repo = Path(repo).resolve()
        self.device = identifier(device)
        self.owner_id = str(uuid.UUID(owner_id))
        self.log = log

    def git(self, *args, check=True, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="ssh -o BatchMode=yes", LC_ALL="C")
        command = ["git", "-c", "core.hooksPath=" + os.devnull, *args]
        try:
            p = subprocess.run(command, cwd=str(cwd or self.repo), env=env, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=300,
                               creationflags=0x08000000 if os.name == "nt" else 0)
        except FileNotFoundError as e:
            raise core.MergeError("找不到 Git，请先安装 Git for Windows") from e
        except subprocess.TimeoutExpired as e:
            raise core.MergeError("Git 操作超时，请检查网络和登录后重试；本地快照保留") from e
        if check and p.returncode:
            detail = (p.stderr or p.stdout).strip()[-2500:]
            raise core.MergeError("Git 操作未完成：" + detail + "\n请确认仓库权限，并使用 Git 凭据管理器或 SSH 登录。")
        return p

    @contextlib.contextmanager
    def locked(self):
        # Uses a separate lock identity from .codex; no lock files enter Git.
        with core.profile_lock(self.repo):
            yield

    def branch(self):
        p = self.git("symbolic-ref", "--short", "HEAD", check=False)
        if p.returncode:
            raise core.MergeError("Git 处于 detached HEAD，请使用独立的普通克隆目录")
        return p.stdout.strip()

    def clean(self):
        if self.git("status", "--porcelain").stdout.strip():
            raise core.MergeError("仓库有未提交文件，请先自行保存或提交；工具不会清理或覆盖它们")
        for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
            if (self.repo / ".git" / name).exists():
                raise core.MergeError("仓库存在未完成的 Git 操作，请先完成后重试")

    def marker(self):
        marker = self.repo / MARKER
        if marker.exists():
            if marker.is_symlink() or core.read_json(marker).get("format") != REPO_FORMAT:
                raise core.MergeError("此目录不是有效的聊天备份仓库")
            return True
        tracked = set(self.git("ls-files").stdout.splitlines())
        if tracked - {"README.md", "LICENSE", ".gitignore", ".gitattributes"}:
            raise core.MergeError("请使用专门的聊天备份私库，不能直接使用已有项目代码仓库")
        return False

    def _open(self):
        if not self.repo.exists() or not any(self.repo.iterdir()):
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            self.log("克隆 Git 聊天仓库……")
            self.git("clone", "--", self.url, str(self.repo), cwd=self.repo.parent)
        if not (self.repo / ".git").is_dir():
            raise core.MergeError("所选本地目录不是独立 Git 克隆；请使用新的专用目录")
        top = Path(self.git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != self.repo:
            raise core.MergeError("请选择仓库根目录")
        if self.git("remote", "get-url", "origin").stdout.strip().rstrip("/") != self.url.rstrip("/"):
            raise core.MergeError("本地 origin 与填写的地址不同，请选择新的克隆目录")
        # A symlink inside the managed tree must never redirect file writes.
        for folder in ("snapshots", "devices"):
            self.safe_repo_path(folder + "/.path-check")
        self.clean()
        self.marker()

    def safe_repo_path(self, rel):
        parts = PurePosixPath(rel)
        if parts.is_absolute() or ".." in parts.parts or "\\" in rel or ":" in rel:
            raise core.MergeError("仓库路径越界")
        p = self.repo.joinpath(*parts.parts)
        for x in [p, *p.parents]:
            if x == self.repo.parent:
                break
            if x.exists() and (x.is_symlink() or getattr(x.lstat(), "st_file_attributes", 0) & 0x400):
                raise core.MergeError("备份仓库路径不支持链接或目录联接")
            if x == self.repo:
                break
        if not p.resolve().is_relative_to(self.repo):
            raise core.MergeError("备份仓库路径越界")
        return p

    def _sync(self):
        self._open()
        self.log("获取远端最新提交……")
        self.git("fetch", "origin")
        has_head = self.git("rev-parse", "--verify", "HEAD", check=False).returncode == 0
        if not has_head:
            refs = self.git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").stdout.splitlines()
            refs = [r for r in refs if r != "origin/HEAD"]
            if refs:
                if len(refs) != 1:
                    raise core.MergeError("远端未设置默认分支且有多个分支，请先在托管平台设置默认分支")
                self.git("switch", "--create", refs[0][len("origin/"):], "--track", refs[0])
            else:
                self.git("symbolic-ref", "HEAD", "refs/heads/codex/chat-sync")
                return
        branch = self.branch()
        remote = "refs/remotes/origin/" + branch
        if self.git("rev-parse", "--verify", remote, check=False).returncode:
            return  # First unpublished commit / no corresponding remote branch yet.
        if self.git("merge-base", "--is-ancestor", "HEAD", remote, check=False).returncode == 0:
            self.git("merge", "--ff-only", remote)
        elif self.git("merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode:
            self._rebase_pending(remote)
        self.marker()
        self.clean()

    def _rebase_pending(self, remote):
        commits = self.git("rev-list", remote + "..HEAD").stdout.splitlines()
        allowed_prefixes = ("snapshots/" + self.device + "/", "devices/" + self.device + "/")
        for commit in commits:
            changed = self.git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit).stdout.splitlines()
            if any(not p.startswith(allowed_prefixes) and p not in {MARKER, ".gitattributes"} for p in changed):
                raise core.MergeError("仓库已分叉且本地含其他文件的提交，请手动处理；不会强制推送")
        self.git("update-ref", "refs/codex-chat-backups/" + core.stamp(), "HEAD")
        self.log("其他电脑已有新提交，正在重放本机快照提交……")
        result = self.git("-c", "rebase.autoStash=false", "-c", "user.name=Codex Chat Backup",
                          "-c", "user.email=codex-chat-backup@localhost", "-c", "commit.gpgSign=false",
                          "rebase", "--no-autosquash", remote, check=False)
        if result.returncode:
            self.git("rebase", "--abort", check=False)
            raise core.MergeError("快照提交产生 Git 冲突，已撤销此次重放并保留本地提交；请手动处理或换用不同设备标识")

    def connect(self):
        with self.locked():
            self._sync()
            return self._list_latest()

    def _list_latest(self):
        result = []
        directory = self.safe_repo_path("devices/.path-check").parent
        if directory.exists():
            for p in sorted(directory.glob("*/latest.json")):
                device = identifier(p.parent.name)
                info = core.read_json(self.safe_repo_path(f"devices/{device}/latest.json"))
                sid = identifier(info["snapshot_id"])
                archive = self.safe_repo_path(f"snapshots/{device}/{sid}/snapshot.json")
                header = core.read_json(archive)
                if header.get("format") != ARCHIVE_FORMAT or header.get("device") != device or header.get("snapshot_id") != sid:
                    raise core.MergeError("远端快照指针与分卷信息不匹配")
                result.append({"device": device, "snapshot_id": sid, "encrypted": header["encrypted"]})
        return result

    def _commit_push(self, paths, message):
        # Always stage exact tool-owned paths; never git add -A.
        self.git("add", "--", *paths)
        # Dedicated backup commits use a local fallback author without altering global config.
        self.git("-c", "user.name=Codex Chat Backup", "-c", "user.email=codex-chat-backup@localhost",
                 "-c", "commit.gpgSign=false", "commit", "-m", message)
        for attempt in range(3):
            result = self.git("push", "--set-upstream", "origin", "HEAD", check=False)
            if not result.returncode:
                return
            self.log("推送未完成，检查远端是否有并发更新……")
            self.git("fetch", "origin")
            remote = "refs/remotes/origin/" + self.branch()
            if self.git("rev-parse", "--verify", remote, check=False).returncode:
                break
            if self.git("merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode == 0:
                break  # This is authentication/network/quota failure, not a competing push.
            self._rebase_pending(remote)
        raise core.MergeError("推送未完成。提交和快照保留在本机，下次连接后可重试。\n" + (result.stderr or result.stdout)[-2000:])

    def upload_snapshot(self, snapshot, password=None):
        snapshot = Path(snapshot).resolve()
        core.disjoint(snapshot, self.repo)
        with self.locked():
            self._sync()
            device_file = self.safe_repo_path(f"devices/{self.device}/device.json")
            if device_file.exists() and core.read_json(device_file).get("owner_id") != self.owner_id:
                raise core.MergeError("此设备标识已被另一台电脑使用，请填写不同的标识，例如 office / home")
            marker = self.marker()
            if marker:
                policy = core.read_json(self.repo / MARKER).get("require_encryption", True)
                if policy and password is None:
                    raise core.MergeError("此备份仓库要求加密，请勾选加密并填写密码")
            # Pack outside Git; interrupted compression cannot dirty the repository.
            with tempfile.TemporaryDirectory(prefix="codex-chat-upload-", dir=self.repo.parent) as tmp:
                packed = Path(tmp) / "packed"
                header = pack_snapshot(snapshot, packed, self.device, password, log=self.log)
                rel = f"snapshots/{self.device}/{header['snapshot_id']}"
                destination = self.safe_repo_path(rel)
                if destination.exists():
                    raise core.MergeError("快照编号重复，请重试")
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Keep creation reversible until the commit; never remove a committed snapshot.
                made = []
                old_pointer = None
                pointer = self.safe_repo_path(f"devices/{self.device}/latest.json")
                old_pointer = pointer.read_bytes() if pointer.exists() else None
                paths = [rel, f"devices/{self.device}/device.json", f"devices/{self.device}/latest.json", MARKER, ".gitattributes"]
                try:
                    made.append(destination)
                    shutil.copytree(packed, destination)
                    if not device_file.exists():
                        core.write_json(device_file, {"device": self.device, "owner_id": self.owner_id})
                        made.append(device_file)
                    core.write_json(pointer, {"snapshot_id": header["snapshot_id"]})
                    if not marker:
                        core.write_json(self.repo / MARKER, {"format": REPO_FORMAT,
                                                           "require_encryption": password is not None})
                        made.append(self.repo / MARKER)
                    attributes = self.repo / ".gitattributes"
                    if not attributes.exists():
                        attributes.write_text("*.bin binary\n*.json text eol=lf\n", encoding="utf-8")
                        made.append(attributes)
                    self.log("提交并推送聊天备份……")
                    self._commit_push(paths, "Backup chats: " + self.device + " " + header["snapshot_id"])
                except BaseException:
                    # Restore only our own files/index entries if no commit captured them.
                    # Failed pushes keep the committed snapshot available for retry.
                    committed = self.git("cat-file", "-e", "HEAD:" + rel + "/snapshot.json", check=False)
                    if committed.returncode:
                        has_head = self.git("rev-parse", "--verify", "HEAD", check=False).returncode == 0
                        if has_head:
                            self.git("reset", "-q", "HEAD", "--", *paths, check=False)
                        else:
                            self.git("rm", "--cached", "--ignore-unmatch", "-r", "--", *paths, check=False)
                        if old_pointer is None:
                            pointer.unlink(missing_ok=True)
                        else:
                            pointer.write_bytes(old_pointer)
                        for p in reversed(made):
                            resolved = p.resolve()
                            if not resolved.is_relative_to(self.repo) or resolved == self.repo:
                                raise core.MergeError("撤销路径越界，已停止")
                            if p.is_dir():
                                shutil.rmtree(p)
                            else:
                                p.unlink(missing_ok=True)
                        self.log("已撤销未提交的仓库文件，本地导出快照保留。")
                    raise
            self.log("已推送：" + self.device + " / " + header["snapshot_id"])
            return header

    def download(self, device, snapshot_id, output, password=None):
        device, snapshot_id = identifier(device), identifier(snapshot_id)
        core.disjoint(self.repo, output)
        with self.locked():
            self._sync()
            folder = self.safe_repo_path(f"snapshots/{device}/{snapshot_id}/snapshot.json").parent
            header = core.read_json(folder / "snapshot.json")
            if header.get("device") != device or header.get("snapshot_id") != snapshot_id:
                raise core.MergeError("快照与请求的设备或编号不匹配")
            return unpack_snapshot(folder, output, password, self.log)
