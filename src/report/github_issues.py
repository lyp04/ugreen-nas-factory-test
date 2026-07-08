"""极薄的 GitHub REST 客户端（纯 urllib，无第三方依赖）。

对应内部的 GitHubIssuesClient，多了 Release 资产上传（用来放含截图的日志 zip，
因为 Issues REST 不支持直接传二进制附件）。只实现本功能用得到的几个端点：
- 按指纹搜索 open issue
- 创建 / 读取 / 整体改写 issue body
- 一次性确保 label 存在
- 确保某 tag 的 release 存在 + 上传资产
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from ..version import VERSION_NAME

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
DEFAULT_TIMEOUT = 30
ASSET_TIMEOUT = 180


class GitHubError(Exception):
    """非 2xx 响应。status 用于上层区分「配置/鉴权错误（丢弃）」与「传输错误（重试）」。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class GitHubIssuesClient:
    def __init__(self, owner: str | None, repo: str | None, token: str | None) -> None:
        self.owner = (owner or "").strip()
        self.repo = (repo or "").strip()
        self.token = (token or "").strip()
        self._labels_ensured = False

    def is_configured(self) -> bool:
        return bool(self.owner and self.repo and self.token)

    # ---------------- low level ----------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: object = None,
        content_type: str = "application/json",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict:
        full = url if url.startswith("http") else f"{API}{url}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ugreen-nas-factory-test/{VERSION_NAME}",
        }
        body: bytes | None = None
        if data is not None:
            if isinstance(data, (bytes, bytearray)):
                body = bytes(data)
                headers["Content-Type"] = content_type
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
        # urllib.request 直接支持自定义 method（含 PATCH），不需要内部那套
        # X-HTTP-Method-Override 隧道。
        request = urllib.request.Request(full, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace") if error.fp else ""
            raise GitHubError(error.code, detail) from error
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    # ---------------- issues ----------------

    def find_issue_by_fingerprint(self, fingerprint: str) -> int | None:
        query = f"repo:{self.owner}/{self.repo} is:issue is:open in:title {fingerprint}"
        url = f"/search/issues?q={urllib.parse.quote(query)}"
        response = self._request("GET", url)
        for item in response.get("items") or []:
            if f"[{fingerprint}]" in (item.get("title") or ""):
                return item.get("number")
        return None

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> int | None:
        self._ensure_labels_once()
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        response = self._request("POST", f"/repos/{self.owner}/{self.repo}/issues", data=payload)
        return response.get("number")

    def get_issue_body(self, number: int) -> str:
        response = self._request("GET", f"/repos/{self.owner}/{self.repo}/issues/{number}")
        return response.get("body") or ""

    def patch_issue_body(self, number: int, body: str) -> None:
        self._request("PATCH", f"/repos/{self.owner}/{self.repo}/issues/{number}", data={"body": body})

    # ---------------- releases / assets ----------------

    def ensure_release(self, tag: str, name: str | None = None) -> dict:
        """按 tag 取 release，没有就建一个（lightweight tag 指向默认分支 HEAD）。"""
        try:
            return self._request(
                "GET", f"/repos/{self.owner}/{self.repo}/releases/tags/{urllib.parse.quote(tag)}"
            )
        except GitHubError as error:
            if error.status != 404:
                raise
        payload = {
            "tag_name": tag,
            "name": name or tag,
            "body": "工厂测试自动故障日志资产桶：每次未分类失败上传一个 zip（含 run.log + test_report.json + 截图）。",
            "prerelease": True,
        }
        return self._request("POST", f"/repos/{self.owner}/{self.repo}/releases", data=payload)

    def upload_release_asset(
        self, release_id: int, asset_name: str, data: bytes, content_type: str = "application/zip"
    ) -> str | None:
        url = (
            f"{UPLOADS}/repos/{self.owner}/{self.repo}/releases/{release_id}/assets"
            f"?name={urllib.parse.quote(asset_name)}"
        )
        response = self._request("POST", url, data=data, content_type=content_type, timeout=ASSET_TIMEOUT)
        return response.get("browser_download_url")

    def _ensure_labels_once(self) -> None:
        if self._labels_ensured:
            return
        self._labels_ensured = True  # 乐观：即便单个 label 建失败也不重试整组
        labels = [
            ("auto:failure", "B60205", "工厂测试自动上报的失败"),
            ("category:other", "5319E7", "未归类（非读写速度/转速/温度/存储池）的故障"),
            ("model:2800", "1D76DB", ""),
            ("model:4800", "0E8A16", ""),
            ("model:4800Plus", "FBCA04", ""),
        ]
        for name, color, description in labels:
            try:
                self._request(
                    "POST",
                    f"/repos/{self.owner}/{self.repo}/labels",
                    data={"name": name, "color": color, "description": description},
                )
            except GitHubError as error:
                if error.status != 422:  # 422 = 已存在
                    pass


__all__ = ["GitHubError", "GitHubIssuesClient", "API", "UPLOADS"]
