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
