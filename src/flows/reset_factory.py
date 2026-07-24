from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from playwright.sync_api import expect

from ..discovery import ugreen_broadcast
from ..utils.desktop import (
    click_desktop_launcher,
    dismiss_desktop_overlays,
    find_visible_locator,
)
from ..utils.logger import logger
from ..utils.sn import extract_full_sn, is_full_sn_candidate, normalize_sn

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page


FRAME_WAIT_MS = 15_000
SHORT_UI_WAIT_MS = 800
FACTORY_RESET_VERIFY_TIMEOUT_S = 900.0
FACTORY_RESET_VERIFY_POLL_MS = 2_000
FACTORY_RESET_VERIFY_STABLE_INTERVAL_S = 10.0
FACTORY_RESET_VERIFY_STABLE_SAMPLES = 2
FACTORY_RESET_VERIFY_PAGE_WAIT_MS = 3_000
FACTORY_RESET_VERIFY_NAV_TIMEOUT_MS = 20_000
FACTORY_RESET_UNCONFIRMED_MARKER = (
    "Factory reset was initiated but completion was not confirmed"
)
FACTORY_RESET_RETRY_BLOCKED_MARKER = (
    "A factory reset from a previous run must not be repeated"
)
SETUP_STRUCTURE_SELECTORS = (
    "button.start-btn",
    "button.next-step-btn",
    'input[placeholder*="设备名"]',
    'input[placeholder*="管理员账号"]',
)
SETUP_TEXT_MARKERS = (
    "未初始化",
    "欢迎使用绿联云存储",
    "命名您的设备",
    "命名您的绿联云",
)
STARTING_TEXT_MARKERS = (
    "服务启动中",
    "您可以尝试手动刷新页面",
    "设备启动中",
    "系统准备中",
)


class FactoryResetError(RuntimeError):
    pass


class FactoryResetCancelled(FactoryResetError):
    """The operator cancelled before the destructive submit was sent."""


class FactoryResetRetryBlocked(FactoryResetError):
    """A prior durable reset checkpoint makes rerunning unsafe."""


class FactoryResetUnconfirmed(FactoryResetError):
    """The destructive submit may have succeeded, but final state is unknown."""


class _ResetIdentityConflict(FactoryResetUnconfirmed):
    """Post-submit discovery exposed contradictory identity fingerprints."""


@dataclass(frozen=True, slots=True)
class FactoryResetResult:
    ip: str
    sn: str
    old_ip_unreachable_observed: bool


@dataclass(frozen=True, slots=True)
class _ResetCandidate:
    ip: str
    identity_confirmed: bool
    mac: str = ""


@dataclass(frozen=True, slots=True)
class _ResetPageState:
    state: str
    observed_sn: str = ""


def run(
    page: "Page",
    selectors: dict,
    admin: dict,
    *,
    nas_url: str,
    expected_sn: str,
    timeout_s: float = FACTORY_RESET_VERIFY_TIMEOUT_S,
    cancel_requested_cb: Callable[[], bool] | None = None,
    on_initiated: Callable[[], None] | None = None,
) -> FactoryResetResult:
    normalized_sn = normalize_sn(expected_sn)
    if not is_full_sn_candidate(normalized_sn):
        raise FactoryResetError("A full NAS serial number is required before factory reset")
    old_ip, scheme, port = _parse_nas_url(nas_url)

    try:
        expected_macs = _initiate(
            page,
            selectors,
            admin,
            expected_sn=normalized_sn,
            expected_ip=old_ip,
            cancel_requested_cb=cancel_requested_cb,
            on_initiated=on_initiated,
        ) or frozenset()
    except FactoryResetUnconfirmed:
        raise
    except Exception as exc:
        if isinstance(exc, FactoryResetError):
            raise
        raise FactoryResetError(f"Could not initiate factory reset: {exc}") from exc

    try:
        return _verify_completion(
            page,
            selectors,
            expected_sn=normalized_sn,
            old_ip=old_ip,
            scheme=scheme,
            port=port,
            timeout_s=timeout_s,
            expected_macs=expected_macs,
            cancel_requested_cb=cancel_requested_cb,
        )
    except FactoryResetUnconfirmed:
        raise
    except Exception as exc:
        raise FactoryResetUnconfirmed(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: {exc}"
        ) from exc


def _initiate(
    page: "Page",
    selectors: dict,
    admin: dict,
    *,
    expected_sn: str,
    expected_ip: str,
    cancel_requested_cb: Callable[[], bool] | None,
    on_initiated: Callable[[], None] | None,
) -> frozenset[str]:
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop = selectors.get("desktop_launchers", {})
    nav = selectors.get("capture_nav", {})
    reset = selectors.get("factory_reset", {})

    logger.info("Post action: restore factory settings")
    frame = _open_app(page, desktop, "ctlmgr")

    update_menu = _require_selector(nav.get("ctlmgr_update_menu"), "capture_nav.ctlmgr_update_menu")
    restore_tab = _require_selector(reset.get("restore_tab"), "factory_reset.restore_tab")
    restore_button = _require_selector(reset.get("restore_button"), "factory_reset.restore_button")
    first_modal = _require_selector(reset.get("first_modal_root"), "factory_reset.first_modal_root")
    acknowledge_checkbox = _require_selector(
        reset.get("first_modal_acknowledge_checkbox"),
        "factory_reset.first_modal_acknowledge_checkbox",
    )
    first_confirm = _require_selector(
        reset.get("first_modal_confirm_button"),
        "factory_reset.first_modal_confirm_button",
    )
    password_modal = _require_selector(reset.get("password_modal_root"), "factory_reset.password_modal_root")
    password_input = _require_selector(reset.get("password_input"), "factory_reset.password_input")
    password_submit = _require_selector(reset.get("password_submit_button"), "factory_reset.password_submit_button")

    frame.locator(update_menu).first.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    tab = frame.locator(restore_tab).first
    expect(tab).to_be_visible(timeout=FRAME_WAIT_MS)
    tab.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    restore = frame.locator(restore_button).first
    expect(restore).to_be_visible(timeout=FRAME_WAIT_MS)
    restore.click()

    first_modal_loc = frame.locator(first_modal).first
    expect(first_modal_loc).to_be_visible(timeout=FRAME_WAIT_MS)

    acknowledge = frame.locator(acknowledge_checkbox).first
    expect(acknowledge).to_be_attached(timeout=FRAME_WAIT_MS)
    if not acknowledge.is_checked():
        try:
            acknowledge.check(force=True, timeout=3_000)
        except Exception:
            # Arco 自绘勾选框把原生 input 移出视口；派发 DOM click 兜底。
            acknowledge.evaluate("el => el.click()")
        expect(acknowledge).to_be_checked(timeout=FRAME_WAIT_MS)

    first_confirm_button = frame.locator(first_confirm).first
    expect(first_confirm_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    first_confirm_button.click()

    password_modal_loc = frame.locator(password_modal).first
    expect(password_modal_loc).to_be_visible(timeout=FRAME_WAIT_MS)

    password_box = frame.locator(password_input).first
    expect(password_box).to_be_visible(timeout=FRAME_WAIT_MS)
    password_box.fill(admin["password"])

    submit_button = frame.locator(password_submit).first
    expect(submit_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    _raise_if_cancelled_before_submit(cancel_requested_cb)
    expected_macs = _verify_reset_target(
        page,
        expected_sn=expected_sn,
        expected_ip=expected_ip,
    )
    _raise_if_cancelled_before_submit(cancel_requested_cb)
    try:
        submit_button.click()
    except Exception as exc:
        # Playwright can lose the frame while the accepted command is already
        # rebooting the NAS. Treat this as uncertain, never as safe-to-retry.
        raise FactoryResetUnconfirmed(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: submit result is unknown"
        ) from exc

    logger.info("Factory reset initiated")
    try:
        if on_initiated is not None:
            on_initiated()
        page.wait_for_timeout(2_000)
    except Exception as exc:
        raise FactoryResetUnconfirmed(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: post-submit checkpoint failed"
        ) from exc
    return expected_macs


def _verify_completion(
    page: "Page",
    selectors: dict,
    *,
    expected_sn: str,
    old_ip: str,
    scheme: str,
    port: int,
    timeout_s: float,
    expected_macs: frozenset[str] = frozenset(),
    cancel_requested_cb: Callable[[], bool] | None = None,
) -> FactoryResetResult:
    probe_context = None
    try:
        browser = page.context.browser
        if browser is None:
            raise RuntimeError("the current browser has no owner browser")
        # Browser.new_context() is an isolated, non-persistent context: cookies,
        # storage, service workers and HTTP cache from the authenticated test
        # session cannot make a stale wizard page look like a completed reset.
        probe_context = browser.new_context(
            locale="zh-CN",
            service_workers="block",
        )
        probe_page = probe_context.new_page()
    except Exception as exc:
        if probe_context is not None:
            try:
                probe_context.close()
            except Exception:
                pass
        raise FactoryResetUnconfirmed(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: could not open a fresh verification page"
        ) from exc

    deadline = time.monotonic() + max(1.0, float(timeout_s))

    def discover_candidates() -> list[_ResetCandidate]:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0.1:
            return []
        return _discover_candidates(
            expected_sn,
            old_ip,
            expected_macs=expected_macs,
            timeout_s=min(1.5, remaining_s),
        )

    def probe_state(ip: str) -> _ResetPageState:
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise TimeoutError("factory reset verification deadline reached")
        cache_buster = int(time.time() * 1000)
        probe_page.goto(
            f"{scheme}://{ip}:{port}/?_factory_reset_verify={cache_buster}",
            wait_until="domcontentloaded",
            timeout=max(1, min(FACTORY_RESET_VERIFY_NAV_TIMEOUT_MS, remaining_ms)),
        )
        _verify_probe_page_host(probe_page, ip)
        remaining_ms = int((deadline - time.monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise TimeoutError("factory reset verification deadline reached")
        probe_page.wait_for_timeout(
            max(1, min(FACTORY_RESET_VERIFY_PAGE_WAIT_MS, remaining_ms))
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("factory reset verification deadline reached")
        return _classify_probe_page(
            probe_page,
            selectors,
            deadline=deadline,
        )

    try:
        return _wait_for_confirmation(
            expected_sn=expected_sn,
            old_ip=old_ip,
            deadline=deadline,
            discover_fn=discover_candidates,
            probe_state_fn=probe_state,
            wait_fn=probe_page.wait_for_timeout,
            cancel_requested_cb=cancel_requested_cb,
            expected_macs=expected_macs,
        )
    finally:
        try:
            probe_context.close()
        except Exception:
            pass


def _wait_for_confirmation(
    *,
    expected_sn: str,
    old_ip: str,
    deadline: float,
    discover_fn: Callable[[], list[_ResetCandidate]],
    probe_state_fn: Callable[[str], _ResetPageState],
    wait_fn: Callable[[int], None],
    cancel_requested_cb: Callable[[], bool] | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    stable_interval_s: float = FACTORY_RESET_VERIFY_STABLE_INTERVAL_S,
    poll_ms: int = FACTORY_RESET_VERIFY_POLL_MS,
    expected_macs: frozenset[str] = frozenset(),
) -> FactoryResetResult:
    expected_sn = normalize_sn(expected_sn)
    stable_ip = ""
    stable_samples = 0
    last_stable_at = 0.0
    old_ip_unreachable_observed = False
    last_observation = "no candidate discovered"

    while now_fn() < deadline:
        if cancel_requested_cb is not None and cancel_requested_cb():
            raise FactoryResetUnconfirmed(
                f"{FACTORY_RESET_UNCONFIRMED_MARKER}: verification cancelled"
            )

        try:
            candidates = discover_fn()
        except _ResetIdentityConflict:
            # A contradictory MAC mapping is positive evidence that discovery
            # cannot safely identify the reset target. Do not downgrade it to
            # the ordinary "discovery unavailable" fallback, especially when
            # no pre-reset MAC was available and visible SN fallback is enabled.
            raise
        except Exception as exc:
            candidates = [_ResetCandidate(old_ip, False)]
            last_observation = f"discovery error: {exc}"
        if cancel_requested_cb is not None and cancel_requested_cb():
            raise FactoryResetUnconfirmed(
                f"{FACTORY_RESET_UNCONFIRMED_MARKER}: verification cancelled"
            )
        if now_fn() >= deadline:
            break

        candidates_to_probe = _dedupe_candidates(candidates, old_ip)

        saw_candidate = False
        confirmed_candidate: _ResetCandidate | None = None
        for candidate in candidates_to_probe:
            if cancel_requested_cb is not None and cancel_requested_cb():
                raise FactoryResetUnconfirmed(
                    f"{FACTORY_RESET_UNCONFIRMED_MARKER}: verification cancelled"
                )
            if now_fn() >= deadline:
                break
            saw_candidate = True
            try:
                state = probe_state_fn(candidate.ip)
            except _ResetIdentityConflict:
                raise
            except Exception as exc:
                if candidate.ip == old_ip:
                    old_ip_unreachable_observed = True
                last_observation = f"{candidate.ip}: probe error: {exc}"
                continue

            if cancel_requested_cb is not None and cancel_requested_cb():
                raise FactoryResetUnconfirmed(
                    f"{FACTORY_RESET_UNCONFIRMED_MARKER}: verification cancelled"
                )
            if now_fn() >= deadline:
                last_observation = f"{candidate.ip}: probe completed after verification deadline"
                break

            observed_sn = normalize_sn(state.observed_sn)
            identity_conflict = bool(observed_sn and observed_sn != expected_sn)
            visible_identity_confirmed = (
                observed_sn == expected_sn and not expected_macs
            )
            identity_confirmed = not identity_conflict and (
                candidate.identity_confirmed
                or visible_identity_confirmed
            )
            last_observation = (
                f"{candidate.ip}: state={state.state}, "
                f"sn={observed_sn or '<not visible>'}, identity={identity_confirmed}"
            )

            if state.state != "setup_wizard" or not identity_confirmed:
                continue

            confirmed_candidate = candidate
            break

        if confirmed_candidate is not None:
            now = now_fn()
            stable_ip = confirmed_candidate.ip
            if stable_samples == 0:
                stable_samples = 1
                last_stable_at = now
            elif now - last_stable_at >= stable_interval_s:
                stable_samples += 1
                last_stable_at = now
            logger.info(
                f"Factory reset verification {stable_ip}: setup wizard confirmed "
                f"({stable_samples}/{FACTORY_RESET_VERIFY_STABLE_SAMPLES})"
            )

            if stable_samples >= FACTORY_RESET_VERIFY_STABLE_SAMPLES:
                logger.info(f"Factory reset confirmed for {expected_sn} at {stable_ip}")
                return FactoryResetResult(
                    stable_ip,
                    expected_sn,
                    old_ip_unreachable_observed,
                )
        else:
            stable_ip = ""
            stable_samples = 0

        if not saw_candidate:
            last_observation = "no matching discovery candidate"
        remaining_ms = int((deadline - now_fn()) * 1_000)
        if remaining_ms <= 0:
            break
        wait_fn(max(1, min(int(poll_ms), remaining_ms)))

    raise FactoryResetUnconfirmed(
        f"{FACTORY_RESET_UNCONFIRMED_MARKER} before timeout; last observation: {last_observation}"
    )


def _verify_probe_page_host(page: "Page", expected_ip: str) -> None:
    try:
        observed_host = urlsplit(str(page.url)).hostname
    except Exception as exc:
        raise _ResetIdentityConflict(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: could not verify the fresh page address"
        ) from exc
    if observed_host != expected_ip:
        raise _ResetIdentityConflict(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: verification page address changed: "
            f"expected {expected_ip}, got {observed_host or '<unknown>'}"
        )


def _discover_candidates(
    expected_sn: str,
    old_ip: str,
    *,
    expected_macs: frozenset[str] = frozenset(),
    timeout_s: float = 1.5,
) -> list[_ResetCandidate]:
    candidates_by_ip: dict[str, _ResetCandidate] = {}
    try:
        hits = ugreen_broadcast.discover(sn=expected_sn, timeout=timeout_s)
    except Exception as exc:
        logger.info(f"Factory reset broadcast verification failed: {exc}")
        hits = []

    matching_hits = [
        hit
        for hit in hits
        if normalize_sn(getattr(hit, "sn", "")) == expected_sn
    ]
    macs_by_ip = _interface_mac_sets(matching_hits)
    conflicts = _interface_mac_conflicts(macs_by_ip)
    if conflicts:
        raise _ResetIdentityConflict(
            f"{FACTORY_RESET_UNCONFIRMED_MARKER}: post-reset broadcast MAC conflict "
            f"for exact SN {expected_sn}: {_format_mac_conflicts(conflicts)}"
        )

    for hit in matching_hits:
        for ip, raw_mac in ugreen_broadcast.interface_macs(hit).items():
            mac = _normalize_mac(raw_mac)
            fingerprint_matches = not expected_macs or mac in expected_macs
            previous = candidates_by_ip.get(ip)
            candidates_by_ip[ip] = _ResetCandidate(
                ip,
                fingerprint_matches
                or bool(previous and previous.identity_confirmed),
                mac or (previous.mac if previous is not None else ""),
            )

    candidates_by_ip.setdefault(old_ip, _ResetCandidate(old_ip, False))
    return sorted(
        candidates_by_ip.values(),
        key=lambda candidate: (candidate.ip != old_ip, candidate.ip),
    )


def _dedupe_candidates(candidates: list[_ResetCandidate], old_ip: str) -> list[_ResetCandidate]:
    merged: dict[str, bool] = {}
    for candidate in candidates:
        if candidate.ip:
            merged[candidate.ip] = merged.get(candidate.ip, False) or candidate.identity_confirmed
    if old_ip and old_ip not in merged:
        merged[old_ip] = False
    ordered = sorted(merged, key=lambda ip: (ip != old_ip, ip))
    return [_ResetCandidate(ip, merged[ip]) for ip in ordered]


def _classify_probe_page(
    page: "Page",
    selectors: dict,
    *,
    deadline: float | None = None,
    now_fn: Callable[[], float] = time.monotonic,
) -> _ResetPageState:
    try:
        surfaces = list(page.frames)
    except Exception:
        surfaces = [page]
    if not surfaces:
        surfaces = [page]

    def remaining_timeout(default_ms: int) -> int:
        if deadline is None:
            return default_ms
        return max(0, min(default_ms, int((deadline - now_fn()) * 1_000)))

    def visible(surface, selector: str) -> bool:
        timeout_ms = remaining_timeout(500)
        return timeout_ms > 0 and _is_visible(
            surface,
            selector,
            timeout_ms=timeout_ms,
        )

    texts = [
        _body_text(surface, timeout_ms=remaining_timeout(1_000))
        for surface in surfaces
    ]
    combined_text = "\n".join(texts)
    observed_sn = extract_full_sn(combined_text) or ""

    login_selectors = selectors.get("login", {}) or {}
    login_page_markers = tuple(
        selector
        for selector in (login_selectors.get("page_marker"),)
        if selector and selector != "TODO"
    )
    password_markers = tuple(
        selector
        for selector in (login_selectors.get("password_input"),)
        if selector and selector != "TODO"
    )
    desktop = selectors.get("desktop_launchers", {}) or {}
    desktop_markers = tuple(
        selector
        for selector in (
            *((desktop.get("apps", {}) or {}).values()),
            *((desktop.get("frames", {}) or {}).values()),
        )
        if selector and selector != "TODO"
    )
    login_page_visible = any(
        visible(surface, selector)
        for surface in surfaces
        for selector in login_page_markers
    )
    password_visible = any(
        visible(surface, selector)
        for surface in surfaces
        for selector in password_markers
    )
    desktop_visible = any(
        visible(surface, selector)
        for surface in surfaces
        for selector in desktop_markers
    )

    setup_cfg = selectors.get("setup_wizard", {}) or {}
    structure_selectors = tuple(
        dict.fromkeys(
            selector
            for selector in (
                setup_cfg.get("page1_next_button"),
                setup_cfg.get("device_name_input"),
                *SETUP_STRUCTURE_SELECTORS,
            )
            if selector and selector != "TODO"
        )
    )
    wizard_visible = any(
        any(marker in text for marker in SETUP_TEXT_MARKERS)
        and any(visible(surface, selector) for selector in structure_selectors)
        for surface, text in zip(surfaces, texts)
    )

    if wizard_visible and not login_page_visible and not desktop_visible:
        return _ResetPageState("setup_wizard", observed_sn)
    if desktop_visible:
        return _ResetPageState("desktop", observed_sn)
    if login_page_visible or password_visible:
        return _ResetPageState("login", observed_sn)
    if not combined_text.strip() or any(marker in combined_text for marker in STARTING_TEXT_MARKERS):
        return _ResetPageState("starting", observed_sn)
    return _ResetPageState("unknown", observed_sn)


def _body_text(surface, *, timeout_ms: int = 1_000) -> str:
    if timeout_ms <= 0:
        return ""
    try:
        return surface.locator("body").inner_text(timeout=timeout_ms)
    except Exception:
        return ""


def _is_visible(surface, selector: str, *, timeout_ms: int = 500) -> bool:
    if timeout_ms <= 0:
        return False
    try:
        return surface.locator(selector).first.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _raise_if_cancelled_before_submit(
    cancel_requested_cb: Callable[[], bool] | None,
) -> None:
    if cancel_requested_cb is not None and cancel_requested_cb():
        raise FactoryResetCancelled("Factory reset cancelled before final submit")


def _verify_reset_target(
    page: "Page",
    *,
    expected_sn: str,
    expected_ip: str,
) -> frozenset[str]:
    try:
        current_host = urlsplit(str(page.url)).hostname
    except Exception as exc:
        raise FactoryResetError(
            "Could not verify the current page address before factory reset"
        ) from exc
    if current_host != expected_ip:
        raise FactoryResetError(
            "Factory reset target address changed before submit: "
            f"expected {expected_ip}, got {current_host or '<unknown>'}"
        )

    try:
        hits = ugreen_broadcast.discover(timeout=1.5)
    except Exception as exc:
        raise FactoryResetError(
            f"Could not verify factory reset target {expected_ip} by broadcast"
        ) from exc

    target_hits = [
        hit
        for hit in hits
        if expected_ip in ugreen_broadcast.interface_macs(hit)
    ]
    observed_sns = {
        normalize_sn(hit.sn)
        for hit in target_hits
        if normalize_sn(hit.sn)
    }
    if observed_sns != {expected_sn}:
        observed = ", ".join(sorted(observed_sns)) or "<not discovered>"
        raise FactoryResetError(
            "Factory reset target identity mismatch: "
            f"{expected_ip} must report exact SN {expected_sn}, got {observed}"
        )

    exact_target_hits = [
        hit
        for hit in target_hits
        if normalize_sn(getattr(hit, "sn", "")) == expected_sn
    ]
    macs_by_ip = _interface_mac_sets(exact_target_hits)
    conflicts = _interface_mac_conflicts(macs_by_ip)
    if conflicts:
        raise FactoryResetError(
            "Factory reset target MAC conflict for exact SN "
            f"{expected_sn}: {_format_mac_conflicts(conflicts)}"
        )
    macs = {
        mac
        for interface_macs in macs_by_ip.values()
        for mac in interface_macs
    }
    if not macs:
        logger.warning(
            f"Factory reset target {expected_ip}/{expected_sn} has no MAC fingerprint; "
            "post-reset verification will rely on the exact SN"
        )
    return frozenset(macs)


def _normalize_mac(value: object) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
    return normalized if len(normalized) == 12 else ""


def _interface_mac_sets(hits: list[object]) -> dict[str, set[str]]:
    macs_by_ip: dict[str, set[str]] = {}
    for hit in hits:
        for ip, raw_mac in ugreen_broadcast.interface_macs(hit).items():
            mac = _normalize_mac(raw_mac)
            if mac:
                macs_by_ip.setdefault(ip, set()).add(mac)
    return macs_by_ip


def _interface_mac_conflicts(
    macs_by_ip: dict[str, set[str]],
) -> dict[str, set[str]]:
    return {
        ip: macs
        for ip, macs in macs_by_ip.items()
        if len(macs) > 1
    }


def _format_mac_conflicts(conflicts: dict[str, set[str]]) -> str:
    return "; ".join(
        f"{ip}=[{', '.join(sorted(macs))}]"
        for ip, macs in sorted(conflicts.items())
    )


def _parse_nas_url(nas_url: str) -> tuple[str, str, int]:
    parsed = urlsplit(nas_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FactoryResetError(f"Invalid NAS URL for factory reset verification: {nas_url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, parsed.scheme, port


def _open_app(page: "Page", desktop_selectors: dict, app: str) -> "Frame":
    app_selector = _require_selector(desktop_selectors.get("apps", {}).get(app), f"desktop_launchers.apps.{app}")
    frame_selector = _require_selector(desktop_selectors.get("frames", {}).get(app), f"desktop_launchers.frames.{app}")

    iframe = page.locator(frame_selector).first
    if iframe.count() and iframe.is_visible():
        _activate_window(page, frame_selector)
    else:
        launcher = find_visible_locator(page, app_selector, FRAME_WAIT_MS)
        if launcher is None:
            raise FactoryResetError(f"Desktop launcher for '{app}' did not appear")
        click_desktop_launcher(page, launcher, app)
        expect(iframe).to_be_visible(timeout=FRAME_WAIT_MS)

    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    frame = _wait_for_frame(page, iframe, app)
    frame.wait_for_load_state("domcontentloaded")
    return frame


def _wait_for_frame(page: "Page", iframe_locator, app: str) -> "Frame":
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        handle = iframe_locator.element_handle()
        frame = handle.content_frame() if handle is not None else None
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise FactoryResetError(f"iframe '{app}' did not appear on the desktop")


def _activate_window(page: "Page", frame_selector: str) -> None:
    window = page.locator(f".cloud-window-main:has({frame_selector})").first
    expect(window).to_be_visible(timeout=FRAME_WAIT_MS)
    window.evaluate(
        """(el) => {
            const windows = Array.from(document.querySelectorAll('.cloud-window-main'));
            const maxZ = windows.reduce((acc, node) => {
                const value = parseInt(getComputedStyle(node).zIndex || '0', 10);
                return Number.isFinite(value) ? Math.max(acc, value) : acc;
            }, 0);
            el.style.zIndex = String(maxZ + 1);
            el.classList.add('focus');
            el.classList.remove('blur');
        }"""
    )
    page.wait_for_timeout(200)
    window.click(position={"x": 120, "y": 24}, force=True)


def _require_selector(selector: str | None, name: str) -> str:
    if not selector or selector == "TODO":
        raise FactoryResetError(f"Selector '{name}' is not configured")
    return selector
