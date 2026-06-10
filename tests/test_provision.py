from src.flows import provision


def test_filemgr_frame_candidates_include_proapp_names_and_src() -> None:
    candidates = provision._frame_selector_candidates('iframe[name="filemgr0"]', "filemgr")

    assert 'iframe[name="filemgr0"]' in candidates
    assert 'iframe[name*="filemgr"]' in candidates
    assert 'iframe[src*="/filemgr/"]' in candidates


def test_expected_pool_disks_respects_model_hdd_count() -> None:
    disks = [f"\u786c\u76d8{index}" for index in range(1, 5)]

    assert provision._expected_pool_disks({"key": "hdd"}, disks, "2800") == disks[:2]
    assert provision._expected_pool_disks({"key": "hdd"}, disks, "4800") == disks
    assert provision._expected_pool_disks({"key": "ssd"}, disks, "2800") == disks


def test_pool_disk_tokens_from_text_keeps_m2_tokens_distinct() -> None:
    text = "238.4GB M.2\u786c\u76d81 238.4GB M.2\u786c\u76d82 \u786c\u76d81 M.2\u786c\u76d81"

    assert provision._pool_disk_tokens_from_text(text) == [
        "M.2\u786c\u76d81",
        "M.2\u786c\u76d82",
        "\u786c\u76d81",
    ]


def test_disk_shortage_confirmation_includes_missing_disks() -> None:
    disks = [f"\u786c\u76d8{index}" for index in range(1, 5)]
    prompts: list[dict] = []

    provision._confirm_disk_shortage_if_needed(
        {"key": "hdd", "pool_name": "\u5b58\u50a8\u6c601"},
        disks,
        disks[:3],
        "RAID 0",
        lambda prompt: prompts.append(prompt) or True,
        "4800",
    )

    assert prompts
    assert prompts[0]["missing_disks"] == [disks[3]]
    assert prompts[0]["fallback_raid"] == "RAID 0"


def test_all_missing_disks_prompts_and_aborts() -> None:
    disks = [f"\u786c\u76d8{index}" for index in range(1, 5)]
    prompts: list[dict] = []

    try:
        provision._confirm_disk_shortage_if_needed(
            {"key": "hdd", "pool_name": "\u5b58\u50a8\u6c601"},
            disks,
            [],
            "RAID 0",
            lambda prompt: prompts.append(prompt) or True,
            "4800Plus",
            visible_disks=["M.2\u786c\u76d81", "M.2\u786c\u76d82"],
        )
    except provision.ProvisionAborted:
        pass
    else:
        raise AssertionError("ProvisionAborted was not raised")

    assert prompts
    assert prompts[0]["can_continue"] is False
    assert prompts[0]["missing_disks"] == disks
    assert prompts[0]["available_disks"] == []
    assert prompts[0]["visible_disks"] == ["M.2\u786c\u76d81", "M.2\u786c\u76d82"]


def test_single_available_disk_uses_basic_when_confirmed() -> None:
    disks = [f"M.2\u786c\u76d8{index}" for index in range(1, 3)]
    prompts: list[dict] = []

    provision._confirm_disk_shortage_if_needed(
        {"key": "ssd", "pool_name": "\u5b58\u50a8\u6c602"},
        disks,
        disks[:1],
        "RAID 0",
        lambda prompt: prompts.append(prompt) or True,
        "2800",
    )

    assert prompts[0]["fallback_raid"] == "Basic"
    assert provision._pool_raid_candidates("RAID 0", selected_disk_count=1)[0] == "Basic"


def test_m2_missing_prompt_aborts_without_retrying_pool_creation() -> None:
    disks = [f"M.2\u786c\u76d8{index}" for index in range(1, 3)]
    prompts: list[dict] = []

    try:
        provision._abort_m2_missing(
            {"key": "ssd", "pool_name": "\u5b58\u50a8\u6c602"},
            disks,
            "RAID 0",
            lambda prompt: prompts.append(prompt) or False,
            "2800",
        )
    except provision.ProvisionAborted as exc:
        assert "M.2" in str(exc)
    else:
        raise AssertionError("ProvisionAborted was not raised")

    assert prompts
    assert prompts[0]["m2_missing"] is True
    assert prompts[0]["can_continue"] is False
    assert prompts[0]["message"] == provision.M2_MISSING_MESSAGE


def test_existing_pools_are_cleaned_before_provisioning(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        provision,
        "_existing_pool_summaries",
        lambda page, selectors: ["RAID 0 硬盘1、2 1.7TB"],
    )
    monkeypatch.setattr(
        provision.cleanup_flow,
        "run",
        lambda page, selectors, admin: calls.append(("cleanup", admin)) or ["pool1"],
    )

    provision._cleanup_existing_pools_before_provisioning("page", {"selectors": True}, {"password": "pw"})

    assert calls == [("cleanup", {"password": "pw"})]


def test_no_existing_pools_skip_pre_provision_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(provision, "_existing_pool_summaries", lambda page, selectors: [])

    def fail_cleanup(*_args, **_kwargs):
        raise AssertionError("cleanup should not run")

    monkeypatch.setattr(provision.cleanup_flow, "run", fail_cleanup)

    provision._cleanup_existing_pools_before_provisioning("page", {}, {})


def test_disk_shortage_can_abort_provisioning() -> None:
    disks = [f"M.2\u786c\u76d8{index}" for index in range(1, 3)]

    try:
        provision._confirm_disk_shortage_if_needed(
            {"key": "ssd", "pool_name": "\u5b58\u50a8\u6c602"},
            disks,
            disks[:1],
            "RAID 0",
            lambda _prompt: False,
            "2800",
        )
    except provision.ProvisionAborted:
        return

    raise AssertionError("ProvisionAborted was not raised")


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakePage:
    def __init__(self) -> None:
        self.keyboard = _FakeKeyboard()

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        pass


class _FakeLocator:
    def __init__(self, frame: "_FakeFrame", name: str, visible: bool = True) -> None:
        self.frame = frame
        self.name = name
        self.visible = visible
        self.clicks = 0

    @property
    def first(self) -> "_FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self.visible else 0

    def is_visible(self, timeout: int | None = None) -> bool:
        return self.visible

    def click(self, **_kwargs) -> None:
        self.clicks += 1
        self.frame.click_log.append(self.name)
        if self.name == "quick":
            self.frame.locators["share"].visible = True
        if self.name == "share_item":
            self.frame.modal_visible = True


class _FakeFrame:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.modal_visible = False
        self.click_log: list[str] = []
        self.locators = {
            "empty": _FakeLocator(self, "empty"),
            "quick": _FakeLocator(self, "quick"),
            "share": _FakeLocator(self, "share_item", visible=False),
            "modal": _FakeLocator(self, "modal", visible=False),
        }

    def locator(self, selector: str) -> _FakeLocator:
        return self.locators[selector]


class _FakeExpectation:
    def __init__(self, locator: _FakeLocator) -> None:
        self.locator = locator

    def to_be_visible(self, timeout: int | None = None) -> None:
        if not self.locator.visible:
            raise AssertionError(f"{self.locator.name} is not visible")


def test_open_share_create_modal_uses_share_menu_after_stale_create_button(monkeypatch) -> None:
    frame = _FakeFrame()
    provision_selectors = {
        "filemgr_empty_create_button": "empty",
        "filemgr_quick_add_button": "quick",
        "filemgr_quick_add_share_item": "share",
        "filemgr_share_modal": "modal",
    }

    monkeypatch.setattr(provision, "expect", lambda locator: _FakeExpectation(locator))
    monkeypatch.setattr(provision, "_dismiss_filemgr_tutorial", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(provision, "_click_visible_text", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        provision,
        "_share_modal_is_visible",
        lambda fake_frame, _selector, timeout_ms: fake_frame.modal_visible,
    )

    provision._open_share_create_modal(frame, provision_selectors)

    assert frame.click_log == ["empty", "quick", "share_item"]


class _FakeMarkerLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self) -> "_FakeMarkerLocator":
        return self

    def is_visible(self, timeout: int | None = None) -> bool:
        return self._visible


class _FakeReloginPage:
    def __init__(self, url: str, login_marker_visible: bool) -> None:
        self.url = url
        self._login_marker_visible = login_marker_visible
        self.waits: list[int] = []

    def locator(self, _selector: str) -> _FakeMarkerLocator:
        return _FakeMarkerLocator(self._login_marker_visible)

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def test_make_relogin_is_noop_off_the_login_page(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(provision.login_flow, "run", lambda *args, **_kwargs: calls.append(args))

    page = _FakeReloginPage("http://10.0.0.5:9999/desktop", login_marker_visible=False)
    relogin = provision._make_relogin(page, {"login": {}}, {"username": "u", "password": "p"})

    assert relogin() is False
    assert calls == []


def test_make_relogin_reauthenticates_at_base_url_on_login_page(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        provision.login_flow,
        "run",
        lambda _page, url, admin, _selectors: calls.append((url, admin)),
    )

    admin = {"username": "u", "password": "p"}
    page = _FakeReloginPage("http://10.0.0.5:9999/some/inner/path", login_marker_visible=True)
    relogin = provision._make_relogin(page, {"login": {"page_marker": ".login-wrapper"}}, admin)

    assert relogin() is True
    # navigates to the bare host, not the deep SPA path it was stuck on
    assert calls == [("http://10.0.0.5:9999", admin)]


def test_relogin_if_on_login_page_delegates_to_thread_context() -> None:
    assert getattr(provision._provision_ctx, "relogin", None) is None
    assert provision._relogin_if_on_login_page() is False  # no active run -> no-op

    seen = {"called": False}

    def fake_relogin() -> bool:
        seen["called"] = True
        return True

    provision._provision_ctx.relogin = fake_relogin
    try:
        assert provision._relogin_if_on_login_page() is True
        assert seen["called"] is True
    finally:
        provision._provision_ctx.relogin = None


def test_relogin_if_on_login_page_swallows_recovery_errors() -> None:
    def boom() -> bool:
        raise RuntimeError("login failed")

    provision._provision_ctx.relogin = boom
    try:
        assert provision._relogin_if_on_login_page() is False
    finally:
        provision._provision_ctx.relogin = None


def test_storage_capacity_gb_parses_largest_value() -> None:
    assert provision._storage_capacity_gb("存储空间1 ext4 可用 1.7TB / 1.7TB") == 1.7 * 1024
    assert provision._storage_capacity_gb("存储空间2 ext4 可用 438GB / 438.1GB") == 438.1
    assert provision._storage_capacity_gb("可用 512MB / 512MB") == 512 / 1024
    assert provision._storage_capacity_gb("存储空间1 ext4") is None


def test_pick_storage_space_maps_by_capacity_not_name() -> None:
    # UGOS swapped the numbering: 存储空间1 is the small M.2 space, 存储空间2 the large HDD one.
    entries = [("存储空间1 可用 438GB", 438.1), ("存储空间2 可用 1.7TB", 1.7 * 1024)]

    # hdd share must land on the large space regardless of which number it carries
    assert provision._pick_storage_space_index(entries, "hdd", "存储空间1", 2, require_full=True) == 1
    # ssd share must land on the small space
    assert provision._pick_storage_space_index(entries, "ssd", "存储空间2", 2, require_full=True) == 0


def test_pick_storage_space_waits_for_all_spaces_when_required() -> None:
    one_space = [("存储空间1 可用 1.7TB", 1.7 * 1024)]
    # only one of two spaces listed so far -> hold out while require_full is set
    assert provision._pick_storage_space_index(one_space, "ssd", "存储空间2", 2, require_full=True) is None
    # last-resort relaxed pick selects among whatever is present
    assert provision._pick_storage_space_index(one_space, "ssd", "存储空间2", 2, require_full=False) == 0


def test_pick_storage_space_falls_back_to_name_for_unknown_key() -> None:
    entries = [("存储空间1", None), ("存储空间2", None)]
    assert provision._pick_storage_space_index(entries, "", "存储空间2", 2, require_full=False) == 1
    assert provision._pick_storage_space_index(entries, "", "缺失空间", 2, require_full=False) is None
