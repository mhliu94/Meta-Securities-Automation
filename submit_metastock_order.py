#!/usr/bin/env python3
"""Submit or cancel MetaStock orders through an Android emulator using ADB.

The script fills the MetaStock trade ticket for a symbol, side, limit price,
and quantity. It is dry-run by default; pass --submit to tap the live
Buy Now/Sell Now button after the ticket is verified.

It can also cancel pending orders for a symbol. Cancellation is dry-run by
default; pass --execute-cancel to tap cancel/confirm controls.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


PACKAGE = "com.metaverse.secu.hk"
REMOTE_XML = "/sdcard/metastock_window.xml"
HOLDINGS_SCREENSHOT = Path("metastock_holdings.png")


def rid(name: str) -> str:
    return f"{PACKAGE}:id/{name}"


STOCK_CODE_ID = rid("tv_stock_code")
ORDER_INPUT_ID = rid("u9")
BUY_TOGGLE_ID = rid("izx")
SELL_TOGGLE_ID = rid("dd6")
BUY_NOW_ID = rid("hx")
SELL_NOW_ID = rid("s2")
SEARCH_RESULT_SYMBOL_ID = rid("ihd")


@dataclass(frozen=True)
class Node:
    element: ET.Element
    parent: ET.Element | None


class AutomationError(RuntimeError):
    pass


@dataclass
class MetaStockFlow:
    adb: str


def find_adb(explicit: str | None) -> str:
    if explicit:
        return explicit

    from_path = shutil.which("adb")
    if from_path:
        return from_path

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
        Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools" / "adb.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise AutomationError("Could not find adb. Pass --adb C:\\path\\to\\adb.exe.")


def run_adb(adb: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [adb, *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise AutomationError(f"adb {' '.join(args)} failed: {detail}")
    return proc


def run_adb_bytes(adb: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        [adb, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
        raise AutomationError(f"adb {' '.join(args)} failed: {detail}")
    return proc


def ensure_device(adb: str) -> None:
    proc = run_adb(adb, ["get-state"], check=False)
    state = proc.stdout.strip()
    if proc.returncode != 0 or state != "device":
        detail = (proc.stderr or proc.stdout).strip() or "no ready device"
        devices = run_adb(adb, ["devices"], check=False).stdout.strip()
        raise AutomationError(
            "No ready Android emulator/device is available. "
            f"adb get-state returned {state or detail!r}. adb devices: {devices}"
        )


def shell(adb: str, *args: str) -> str:
    return run_adb(adb, ["shell", *args]).stdout


def dump_ui(adb: str) -> ET.Element:
    run_adb(adb, ["shell", "rm", "-f", REMOTE_XML], check=False)
    shell(adb, "uiautomator", "dump", REMOTE_XML)
    xml_text = run_adb(adb, ["exec-out", "cat", REMOTE_XML]).stdout
    if "<hierarchy" not in xml_text and "<displays" not in xml_text:
        raise AutomationError(f"UI hierarchy dump did not produce XML: {xml_text.strip()}")
    root = ET.fromstring(xml_text)
    raise_if_app_crashed(root)
    return root


def dump_ui_compressed(adb: str) -> ET.Element:
    run_adb(adb, ["shell", "rm", "-f", REMOTE_XML], check=False)
    shell(adb, "uiautomator", "dump", "--compressed", REMOTE_XML)
    xml_text = run_adb(adb, ["exec-out", "cat", REMOTE_XML]).stdout
    if "<hierarchy" not in xml_text and "<displays" not in xml_text:
        raise AutomationError(f"Compressed UI hierarchy dump did not produce XML: {xml_text.strip()}")
    root = ET.fromstring(xml_text)
    raise_if_app_crashed(root)
    return root


def pull_screenshot(adb: str, local_path: Path) -> None:
    remote_path = "/sdcard/metastock_holdings_ocr.png"
    shell(adb, "screencap", "-p", remote_path)
    run_adb(adb, ["pull", remote_path, str(local_path)])


def capture_screenshot(adb: str, local_path: Path) -> None:
    proc = run_adb_bytes(adb, ["exec-out", "screencap", "-p"])
    local_path.write_bytes(proc.stdout)


def iter_nodes(root: ET.Element) -> list[Node]:
    found: list[Node] = []

    def walk(element: ET.Element, parent: ET.Element | None) -> None:
        if element.tag == "node":
            found.append(Node(element, parent))
        for child in element:
            walk(child, element)

    walk(root, None)
    return found


def attr(node: ET.Element, name: str) -> str:
    return node.attrib.get(name, "")


def bounds(node: ET.Element) -> tuple[int, int, int, int]:
    raw = attr(node, "bounds")
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw)
    if not match:
        raise AutomationError(f"Node has invalid bounds: {raw!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def center(node: ET.Element) -> tuple[int, int]:
    left, top, right, bottom = bounds(node)
    return (left + right) // 2, (top + bottom) // 2


def tap(adb: str, node: ET.Element) -> None:
    x, y = center(node)
    shell(adb, "input", "tap", str(x), str(y))


def tap_xy(adb: str, x: int, y: int) -> None:
    shell(adb, "input", "tap", str(x), str(y))


def swipe_xy(adb: str, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 300) -> None:
    shell(adb, "input", "swipe", str(start_x), str(start_y), str(end_x), str(end_y), str(duration_ms))


def wait_for_ui(adb: str, predicate, *, timeout: float = 8.0, interval: float = 0.4) -> ET.Element:
    deadline = time.time() + timeout
    last_root = None
    while time.time() < deadline:
        root = dump_ui(adb)
        last_root = root
        if predicate(root):
            return root
        time.sleep(interval)
    if last_root is not None:
        return last_root
    raise AutomationError("Timed out before a UI hierarchy could be read.")


def find_first(root: ET.Element, **criteria: str) -> ET.Element | None:
    for node in root.iter("node"):
        if all(attr(node, key) == value for key, value in criteria.items()):
            return node
    return None


def find_by_text(root: ET.Element, text: str) -> ET.Element | None:
    return find_first(root, text=text)


def find_by_id(root: ET.Element, resource_id: str) -> ET.Element | None:
    return find_first(root, **{"resource-id": resource_id})


def find_by_id_or_text(root: ET.Element, resource_id: str, text: str) -> ET.Element | None:
    return find_by_id(root, resource_id) or find_by_text(root, text)


def find_all_by_text(root: ET.Element, text: str) -> list[ET.Element]:
    return [node for node in root.iter("node") if attr(node, "text") == text]


def find_text_in(root: ET.Element, labels: list[str]) -> ET.Element | None:
    for label in labels:
        node = find_by_text(root, label)
        if node is not None:
            return node
    return None


def visible_texts(root: ET.Element) -> list[str]:
    seen: set[str] = set()
    texts: list[str] = []
    for node in root.iter("node"):
        for field in ("text", "content-desc", "hint"):
            value = attr(node, field).strip()
            if value and value not in seen:
                seen.add(value)
                texts.append(value)
    return texts


def summarize_app_text(root: ET.Element, *, limit: int = 40) -> str:
    ignored = {
        "Trade",
        "Order",
        "Account",
        "News",
        "Watchlist",
        "Buy",
        "Sell",
        "Limit Order",
        "Enhanced Limit",
        "Input price",
        "Input quantity",
        "Buy Now",
        "Sell Now",
    }
    useful = [
        text
        for text in visible_texts(root)
        if text not in ignored and not re.fullmatch(r"\d{1,2}:\d{2}", text)
    ]
    if not useful:
        return "(no readable app text)"
    return " | ".join(useful[:limit])


def has_any_text(root: ET.Element, labels: list[str]) -> bool:
    texts = set(visible_texts(root))
    return any(label in texts for label in labels)


def raise_if_app_crashed(root: ET.Element) -> None:
    text = " ".join(visible_texts(root)).lower()
    crash_markers = [
        "keeps stopping",
        "has stopped",
        "isn't responding",
        "is not responding",
        "close app",
        "app info",
        "wait",
    ]
    if "metastock" in text and any(marker in text for marker in crash_markers):
        raise AutomationError(f"MetaStock appears to have crashed or stopped responding: {summarize_app_text(root)}")


def is_login_required(root: ET.Element) -> bool:
    text = " ".join(visible_texts(root)).lower()
    markers = [
        "login/register",
        "log in",
        "login",
        "sign in",
        "please login",
        "please log in",
    ]
    return any(marker in text for marker in markers)


def raise_if_login_required(root: ET.Element) -> None:
    if is_login_required(root):
        raise AutomationError(f"MetaStock trading account is not logged in: {summarize_app_text(root)}")


def has_metastock_marker(root: ET.Element) -> bool:
    markers = [
        "Trade",
        "Order",
        "Account",
        "Watchlist",
        "Login/Register",
        "Limit Order",
        "Enhanced Limit",
        "US Account(USD)",
        "HK Account(HKD)",
    ]
    return has_any_text(root, markers)


def find_clickable_text(root: ET.Element, labels: list[str]) -> ET.Element | None:
    for label in labels:
        for found in iter_nodes(root):
            node = found.element
            if attr(node, "text") != label and attr(node, "content-desc") != label:
                continue
            if attr(node, "clickable") == "true":
                return node
            if found.parent is not None and attr(found.parent, "clickable") == "true":
                return found.parent
    return None


def find_edit_by_hint(root: ET.Element, hint: str) -> ET.Element | None:
    for node in root.iter("node"):
        if attr(node, "class") == "android.widget.EditText" and attr(node, "hint") == hint:
            return node
    return None


def find_order_input(root: ET.Element, hint: str) -> ET.Element | None:
    for node in root.iter("node"):
        if (
            attr(node, "resource-id") == ORDER_INPUT_ID
            and attr(node, "class") == "android.widget.EditText"
            and attr(node, "hint") == hint
        ):
            return node
    return find_edit_by_hint(root, hint)


def find_order_inputs(root: ET.Element) -> tuple[ET.Element | None, ET.Element | None]:
    price_field = find_order_input(root, "Input price")
    qty_field = find_order_input(root, "Input quantity")
    if price_field is not None and qty_field is not None:
        return price_field, qty_field

    fields = [
        node
        for node in root.iter("node")
        if attr(node, "resource-id") == ORDER_INPUT_ID and attr(node, "class") == "android.widget.EditText"
    ]
    if len(fields) >= 2:
        return price_field or fields[0], qty_field or fields[1]
    return price_field, qty_field


def has_trade_ticket(root: ET.Element) -> bool:
    has_submit = find_by_id(root, BUY_NOW_ID) is not None or find_by_id(root, SELL_NOW_ID) is not None
    price_field, _ = find_order_inputs(root)
    if has_submit and price_field is not None:
        return True
    has_type = find_by_text(root, "Limit Order") is not None or find_by_text(root, "Enhanced Limit") is not None
    return has_type and price_field is not None


def has_order_page(root: ET.Element) -> bool:
    return (
        find_by_text(root, "Order") is not None
        and find_by_text(root, "Symbol") is not None
        and find_by_text(root, "Status") is not None
    )


def has_account_page(root: ET.Element) -> bool:
    return (
        find_by_text(root, "Net Asset Value") is not None
        or find_by_text(root, "US Account(USD)") is not None
        or find_by_text(root, "HK Account(HKD)") is not None
    )


def dismiss_order_overlay_if_present(adb: str, root: ET.Element) -> ET.Element:
    overlay_markers = [
        "Enter stock symbol / name",
        "Order Status",
    ]
    if not any(find_by_text(root, marker) is not None for marker in overlay_markers):
        return root

    cancel = find_by_text(root, "Cancel")
    if cancel is None:
        return root

    tap(adb, cancel)
    time.sleep(0.5)
    return dump_ui(adb)


def foreground_package(adb: str) -> str | None:
    output = shell(adb, "dumpsys", "window")
    patterns = [
        r"mCurrentFocus=.*? ([A-Za-z0-9_.]+)/",
        r"mFocusedApp=.*? ([A-Za-z0-9_.]+)/",
        r"mObscuringWindow=.*? ([A-Za-z0-9_.]+)/",
    ]
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def launch_metastock(adb: str) -> None:
    run_adb(adb, ["shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"])
    deadline = time.time() + 8.0
    last_package: str | None = None
    last_text = "(no UI hierarchy)"
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            last_package = foreground_package(adb)
            root = dump_ui(adb)
            last_text = summarize_app_text(root)
        except (AutomationError, ET.ParseError) as exc:
            last_text = str(exc)
            continue
        if last_package == PACKAGE or has_metastock_marker(root):
            return
    raise AutomationError(
        "MetaStock did not come to the foreground after launch. "
        f"Foreground package: {last_package or 'unknown'}. Last visible text: {last_text}"
    )


def launch_metastock_for_ocr(adb: str) -> None:
    run_adb(adb, ["shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"])
    deadline = time.time() + 8.0
    last_package: str | None = None
    while time.time() < deadline:
        time.sleep(0.4)
        last_package = foreground_package(adb)
        if last_package == PACKAGE:
            return
    raise AutomationError(f"MetaStock did not come to the foreground after launch. Foreground package: {last_package or 'unknown'}.")


def require_logged_in(root: ET.Element) -> None:
    raise_if_login_required(root)


def ensure_trade_ticket(adb: str) -> ET.Element:
    launch_metastock(adb)
    root = dump_ui(adb)
    require_logged_in(root)
    root = dismiss_order_overlay_if_present(adb, root)
    require_logged_in(root)
    came_from_order_page = False
    if has_trade_ticket(root):
        return root
    if has_order_page(root):
        shell(adb, "input", "keyevent", "BACK")
        time.sleep(1.0)
        root = dump_ui(adb)
        came_from_order_page = True

    trade_candidates = find_all_by_text(root, "Trade")
    trade = None
    for candidate in trade_candidates:
        _, top, _, bottom = bounds(candidate)
        y = (top + bottom) // 2
        if y > 500:
            trade = candidate
            break
    if trade is None and trade_candidates:
        trade = trade_candidates[0]
    if trade is None:
        account_markers = ["Net Asset Value", "US Account(USD)", "HK Account(HKD)"]
        if came_from_order_page or any(find_by_text(root, marker) is not None for marker in account_markers):
            tap_xy(adb, 132, 916)
            root = wait_for_ui(adb, has_trade_ticket, timeout=8.0)
            require_logged_in(root)
            return root
        if has_metastock_marker(root):
            raise AutomationError(
                "MetaStock is open, but the Trade control is not available. "
                f"The trading account may be logged out or trading access is unavailable. Visible app text: {summarize_app_text(root)}"
            )
        raise AutomationError("MetaStock is open, but the Trade control was not found.")

    tap(adb, trade)
    root = wait_for_ui(adb, has_trade_ticket)
    require_logged_in(root)
    return root


def escape_input_text(text: str) -> str:
    return text.replace(" ", "%s")


def clear_and_type(adb: str, node: ET.Element, value: str, *, delete_count: int = 30) -> None:
    tap(adb, node)
    shell(adb, "input", "keyevent", "KEYCODE_MOVE_END")
    for _ in range(delete_count):
        shell(adb, "input", "keyevent", "KEYCODE_DEL")
    shell(adb, "input", "text", escape_input_text(value))
    time.sleep(0.2)


def clear_and_type_search(adb: str, node: ET.Element, value: str) -> None:
    clear_and_type(adb, node, value)
    shell(adb, "input", "keyevent", "KEYCODE_ENTER")
    time.sleep(0.5)


def normalize_symbol(symbol: str) -> tuple[str, str]:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise AutomationError("Symbol cannot be empty.")
    search = cleaned.split(".", 1)[0]
    expected = cleaned if "." in cleaned else f"{cleaned}.US"
    return search, expected


def open_symbol_search(adb: str, root: ET.Element) -> ET.Element:
    stock_code = find_by_id(root, STOCK_CODE_ID)
    if stock_code is not None:
        tap(adb, stock_code)
        return wait_for_ui(adb, lambda r: find_edit_by_hint(r, "Enter stock symbol / name") is not None)

    empty_selector = find_by_text(root, "Enter stock symbol / name")
    if empty_selector is None:
        raise AutomationError("Could not find current stock selector on the trade ticket.")

    tap(adb, empty_selector)
    return wait_for_ui(adb, lambda r: find_edit_by_hint(r, "Enter stock symbol / name") is not None)


def choose_symbol(adb: str, symbol: str) -> ET.Element:
    search_text, expected_symbol = normalize_symbol(symbol)

    root = ensure_trade_ticket(adb)
    current = find_by_id(root, STOCK_CODE_ID)
    if current is not None and attr(current, "text").upper() == expected_symbol:
        return root

    root = open_symbol_search(adb, root)
    search = find_edit_by_hint(root, "Enter stock symbol / name")
    if search is None:
        raise AutomationError("Symbol search field did not appear.")

    clear_and_type_search(adb, search, search_text)

    def exact_result_visible(r: ET.Element) -> bool:
        for node in r.iter("node"):
            if attr(node, "resource-id") == SEARCH_RESULT_SYMBOL_ID and attr(node, "text").upper() == expected_symbol:
                return True
        return False

    root = wait_for_ui(adb, exact_result_visible, timeout=10.0)
    for node in root.iter("node"):
        if attr(node, "resource-id") == SEARCH_RESULT_SYMBOL_ID and attr(node, "text").upper() == expected_symbol:
            left, top, right, bottom = bounds(node)
            tap_xy(adb, 300, (top + bottom) // 2)
            break
    else:
        raise AutomationError(f"Could not find exact search result for {expected_symbol}.")

    def ticket_has_symbol(r: ET.Element) -> bool:
        code = find_by_id(r, STOCK_CODE_ID)
        return has_trade_ticket(r) and code is not None and attr(code, "text").upper() == expected_symbol

    return wait_for_ui(adb, ticket_has_symbol, timeout=10.0)


def set_direction(adb: str, root: ET.Element, side: str) -> ET.Element:
    label = "Buy" if side.upper() == "BUY" else "Sell"
    toggle_id = BUY_TOGGLE_ID if side.upper() == "BUY" else SELL_TOGGLE_ID
    node = find_by_id_or_text(root, toggle_id, label)
    if node is None:
        raise AutomationError(f"Could not find {label} direction toggle.")
    tap(adb, node)
    return wait_for_ui(adb, lambda r: (find_by_id_or_text(r, toggle_id, label) is not None), timeout=4.0)


def fill_order(adb: str, root: ET.Element, price: str, quantity: str) -> ET.Element:
    price_field, qty_field = find_order_inputs(root)
    if price_field is None or qty_field is None:
        raise AutomationError("Could not find price and quantity inputs.")

    clear_and_type(adb, price_field, price)
    root = dump_ui(adb)
    _, qty_field = find_order_inputs(root)
    if qty_field is None:
        raise AutomationError("Quantity input disappeared after entering price.")
    clear_and_type(adb, qty_field, quantity)
    return dump_ui(adb)


def verify_ticket(root: ET.Element, symbol: str, side: str, price: str, quantity: str) -> ET.Element:
    _, expected_symbol = normalize_symbol(symbol)
    code = find_by_id(root, STOCK_CODE_ID)
    if code is None or attr(code, "text").upper() != expected_symbol:
        raise AutomationError(f"Expected symbol {expected_symbol}, found {attr(code, 'text') if code is not None else 'none'}.")

    direction_id = BUY_TOGGLE_ID if side.upper() == "BUY" else SELL_TOGGLE_ID
    direction = find_by_id_or_text(root, direction_id, "Buy" if side.upper() == "BUY" else "Sell")
    if direction is None or attr(direction, "selected") != "true":
        raise AutomationError(f"Expected {side.upper()} direction to be selected.")

    price_field, qty_field = find_order_inputs(root)
    if price_field is None or attr(price_field, "text") != price:
        raise AutomationError(f"Expected price {price}, found {attr(price_field, 'text') if price_field is not None else 'none'}.")
    if qty_field is None or attr(qty_field, "text") != quantity:
        raise AutomationError(f"Expected quantity {quantity}, found {attr(qty_field, 'text') if qty_field is not None else 'none'}.")

    button_label = "Buy Now" if side.upper() == "BUY" else "Sell Now"
    button_id = BUY_NOW_ID if side.upper() == "BUY" else SELL_NOW_ID
    button = find_by_id_or_text(root, button_id, button_label)
    if button is None:
        raise AutomationError(f"Expected final button {button_label!r} was not found.")
    return button


CONFIRM_BUTTON_LABELS = [
    "Confirm",
    "Submit",
    "Place Order",
    "Confirm Order",
    "OK",
]

SUCCESS_KEYWORDS = [
    "success",
    "successful",
    "submitted",
    "placed",
    "accepted",
]

FAILURE_KEYWORDS = [
    "fail",
    "failed",
    "failure",
    "error",
    "reject",
    "rejected",
    "invalid",
    "insufficient",
    "not enough",
    "fund",
    "funds",
    "unable",
    "cannot",
    "can't",
    "wrong",
    "incorrect",
    "exceed",
    "minimum",
    "maximum",
    "suspended",
    "closed",
    "trading hour",
    "trading hours",
    "outside trading",
    "market closed",
    "market is closed",
    "session",
    "unavailable",
    "permission",
    "buying power",
    "cash balance",
    "position",
    "network",
]

NOTICE_LABELS = [
    "Alert",
    "Error",
    "Failed",
    "Failure",
    "Notice",
    "Prompt",
    "Reminder",
    "Tips",
    "Warning",
]

PASSWORD_CONFIRM_BUTTON_LABELS = [
    "Confirm",
    "Submit",
    "OK",
    "Done",
]


def confirmation_button_labels(side: str) -> list[str]:
    return list(CONFIRM_BUTTON_LABELS)


def looks_like_order_confirmation(root: ET.Element, side: str) -> bool:
    texts = set(visible_texts(root))
    if texts.intersection({"Order Confirmation", "Confirm Order", "Confirm"}):
        return True
    detail_markers = {
        "Symbol",
        "Order Type",
        "Price",
        "Quantity",
        "Amount",
        "Account",
        "Validity",
    }
    side_text = "Buy" if side.upper() == "BUY" else "Sell"
    return side_text in texts and len(texts.intersection(detail_markers)) >= 2


def find_order_confirmation_control(root: ET.Element, side: str) -> ET.Element | None:
    if not looks_like_order_confirmation(root, side):
        return None
    return find_clickable_text(root, confirmation_button_labels(side))


def classify_submit_result(root: ET.Element) -> str | None:
    text = " ".join(visible_texts(root)).lower()
    if any(keyword in text for keyword in FAILURE_KEYWORDS):
        return "failure"
    if any(keyword in text for keyword in SUCCESS_KEYWORDS):
        return "success"
    if find_clickable_text(root, ["OK", "Confirm"]) is not None:
        if any(label.lower() in text for label in NOTICE_LABELS):
            return "failure"
    return None


def find_trading_password_field(root: ET.Element) -> ET.Element | None:
    for node in root.iter("node"):
        if attr(node, "class") != "android.widget.EditText":
            continue
        text_blob = " ".join(
            attr(node, field).lower()
            for field in ("text", "content-desc", "hint", "resource-id")
        )
        if attr(node, "password") == "true" or "password" in text_blob:
            return node
    return None


def has_trading_password_prompt(root: ET.Element) -> bool:
    field = find_trading_password_field(root)
    if field is None:
        return False
    text = " ".join(visible_texts(root)).lower()
    password_markers = ["password", "trading password", "transaction password"]
    return any(marker in text for marker in password_markers) or attr(field, "password") == "true"


def get_trading_password(
    configured_password: str | None,
    *,
    prompt: bool,
    env_name: str,
) -> str:
    if configured_password:
        return configured_password
    if prompt:
        password = getpass.getpass("MetaStock trading password: ")
        if password:
            return password
    raise AutomationError(
        "MetaStock requested the trading password before submitting. "
        f"Set {env_name}, pass --trading-password, or use --prompt-trading-password."
    )


def submit_trading_password(
    adb: str,
    root: ET.Element,
    *,
    configured_password: str | None,
    prompt: bool,
    env_name: str,
) -> None:
    field = find_trading_password_field(root)
    if field is None:
        raise AutomationError(f"Trading password prompt was detected, but no password field was found: {summarize_app_text(root)}")

    password = get_trading_password(configured_password, prompt=prompt, env_name=env_name)
    clear_and_type(adb, field, password, delete_count=max(30, len(password) + 5))
    root = dump_ui(adb)
    confirm = find_clickable_text(root, PASSWORD_CONFIRM_BUTTON_LABELS)
    if confirm is None:
        raise AutomationError(f"Trading password was entered, but no submit control was found: {summarize_app_text(root)}")
    tap(adb, confirm)
    print("Trading password submitted.")


def wait_for_order_confirmation(adb: str, side: str, timeout: float) -> tuple[ET.Element, ET.Element]:
    deadline = time.time() + timeout
    last_root: ET.Element | None = None
    while time.time() < deadline:
        root = dump_ui(adb)
        last_root = root
        raise_if_login_required(root)
        control = find_order_confirmation_control(root, side)
        if control is not None:
            return root, control
        if classify_submit_result(root) == "failure":
            raise AutomationError(f"Order was rejected before confirmation: {summarize_app_text(root)}")
        time.sleep(0.3)

    detail = summarize_app_text(last_root) if last_root is not None else "(no UI hierarchy)"
    raise AutomationError(f"Confirmation dialog did not appear. Last app text: {detail}")


def wait_for_submit_result(
    adb: str,
    timeout: float,
    *,
    trading_password: str | None,
    prompt_trading_password: bool,
    trading_password_env: str,
) -> tuple[str | None, ET.Element]:
    deadline = time.time() + timeout
    last_root = dump_ui(adb)
    password_attempted = False
    while time.time() < deadline:
        root = dump_ui(adb)
        last_root = root
        raise_if_login_required(root)
        if has_trading_password_prompt(root):
            if password_attempted:
                raise AutomationError(f"Trading password was rejected or requested again: {summarize_app_text(root)}")
            submit_trading_password(
                adb,
                root,
                configured_password=trading_password,
                prompt=prompt_trading_password,
                env_name=trading_password_env,
            )
            password_attempted = True
            time.sleep(0.8)
            continue
        outcome = classify_submit_result(root)
        if outcome is not None:
            return outcome, root
        time.sleep(0.4)
    return None, last_root


def submit_order_with_confirmation(
    adb: str,
    final_button: ET.Element,
    side: str,
    *,
    confirm_timeout: float,
    result_timeout: float,
    trading_password: str | None,
    prompt_trading_password: bool,
    trading_password_env: str,
) -> None:
    tap(adb, final_button)
    print(f"Tapped {attr(final_button, 'text')!r}; waiting for MetaStock confirmation.")

    confirmation_root, confirm_button = wait_for_order_confirmation(adb, side, confirm_timeout)
    print(f"Confirmation shown: {summarize_app_text(confirmation_root)}")

    tap(adb, confirm_button)
    print(f"Tapped confirmation control {attr(confirm_button, 'text') or attr(confirm_button, 'content-desc')!r}.")

    outcome, result_root = wait_for_submit_result(
        adb,
        result_timeout,
        trading_password=trading_password,
        prompt_trading_password=prompt_trading_password,
        trading_password_env=trading_password_env,
    )
    message = summarize_app_text(result_root)
    if outcome == "failure":
        raise AutomationError(f"Order submission failed: {message}")
    if outcome == "success":
        print(f"Order submission reported success: {message}")
        return

    print(f"Order confirmation submitted; no failure message appeared within {result_timeout:.1f}s.")
    print(f"Last visible app text: {message}")


class SubmitNewOrderFlow(MetaStockFlow):
    def prepare_ticket(self, symbol: str, side: str, price: str, quantity: str) -> ET.Element:
        side = side.upper()
        root = choose_symbol(self.adb, symbol)
        root = set_direction(self.adb, root, side)
        root = fill_order(self.adb, root, price, quantity)
        return verify_ticket(root, symbol, side, price, quantity)

    def run(
        self,
        symbol: str,
        side: str,
        price: str,
        quantity: str,
        *,
        submit: bool,
        confirm_timeout: float,
        result_timeout: float,
        trading_password: str | None,
        trading_password_env: str,
        prompt_trading_password: bool,
    ) -> None:
        side = side.upper()
        final_button = self.prepare_ticket(symbol, side, price, quantity)

        print(f"Ticket verified: {side} {normalize_symbol(symbol)[1]} {quantity} @ {price}")

        if not submit:
            print("Dry run only. Re-run with --submit to tap the live order button.")
            return

        submit_order_with_confirmation(
            self.adb,
            final_button,
            side,
            confirm_timeout=confirm_timeout,
            result_timeout=result_timeout,
            trading_password=trading_password,
            prompt_trading_password=prompt_trading_password,
            trading_password_env=trading_password_env,
        )


def open_account_page(adb: str) -> ET.Element:
    launch_metastock(adb)
    root = dump_ui_compressed(adb)
    root = dismiss_order_overlay_if_present(adb, root)
    if has_account_page(root):
        return dump_ui_compressed(adb)

    if has_order_page(root) or has_trade_ticket(root):
        for _ in range(3):
            shell(adb, "input", "keyevent", "BACK")
            time.sleep(0.8)
            root = dump_ui_compressed(adb)
            if has_account_page(root):
                return root
            if not has_order_page(root) and not has_trade_ticket(root):
                break

    account = find_by_text(root, "Account")
    if account is not None:
        tap(adb, account)
    else:
        tap_xy(adb, 756, 2295)
    time.sleep(1.5)

    root = dump_ui_compressed(adb)
    if not has_account_page(root):
        raise AutomationError("Could not open the Account page for holdings.")
    return root


def open_us_account_detail_page(adb: str) -> ET.Element:
    launch_metastock(adb)

    for _ in range(3):
        try:
            root = dump_ui_compressed(adb)
            if find_by_text(root, "US Account") is not None and find_by_text(root, "Net Liquidation Value (USD)") is not None:
                return root
            if has_order_page(root) or has_trade_ticket(root):
                shell(adb, "input", "keyevent", "BACK")
                time.sleep(0.8)
                continue
        except AutomationError:
            pass
        break

    tap_xy(adb, 756, 2295)
    time.sleep(1.5)
    tap_xy(adb, 250, 1515)
    time.sleep(2.0)
    root = dump_ui_compressed(adb)
    if find_by_text(root, "US Account") is None or find_by_text(root, "Net Liquidation Value (USD)") is None:
        raise AutomationError("Could not open the US Account holdings detail page.")
    return root


def first_text_near(root: ET.Element, resource_id: str, top: int, bottom: int) -> str:
    for node in root.iter("node"):
        if attr(node, "resource-id") != resource_id:
            continue
        _, node_top, _, node_bottom = bounds(node)
        node_center = (node_top + node_bottom) // 2
        if top <= node_center <= bottom:
            return attr(node, "text")
    return ""


def texts_by_id(root: ET.Element, resource_id: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    values = []
    for node in root.iter("node"):
        if attr(node, "resource-id") == resource_id:
            values.append((attr(node, "text"), bounds(node)))
    return values


def collect_holdings_snapshot(root: ET.Element) -> dict:
    account_type = attr(find_first(root, **{"resource-id": f"{PACKAGE}:id/irk"}) or ET.Element("node"), "text")
    account_number = attr(find_first(root, **{"resource-id": f"{PACKAGE}:id/xv"}) or ET.Element("node"), "text")
    nav_currency = attr(find_first(root, **{"resource-id": f"{PACKAGE}:id/ac6"}) or ET.Element("node"), "text")

    net_asset_value = ""
    for node in root.iter("node"):
        if attr(node, "resource-id") == f"{PACKAGE}:id/ip1":
            _, top, _, _ = bounds(node)
            if top < 800:
                net_asset_value = attr(node, "text")
                break

    cash_accounts = []
    for account_node in root.iter("node"):
        if attr(account_node, "resource-id") != f"{PACKAGE}:id/i8":
            continue
        _, top, _, bottom = bounds(account_node)
        card_top = max(0, top - 70)
        card_bottom = min(2424, bottom + 180)
        cash_accounts.append(
            {
                "account": attr(account_node, "text"),
                "value": first_text_near(root, f"{PACKAGE}:id/ip1", card_top, card_bottom),
                "daily_pnl": first_text_near(root, f"{PACKAGE}:id/iva", card_top, card_bottom),
                "total_pnl": first_text_near(root, f"{PACKAGE}:id/a3t", card_top, card_bottom),
            }
        )

    positions = []
    for symbol_node in root.iter("node"):
        if attr(symbol_node, "resource-id") != f"{PACKAGE}:id/iwb":
            continue
        _, top, _, bottom = bounds(symbol_node)
        row_top = max(0, top - 90)
        row_bottom = min(2424, bottom + 80)
        positions.append(
            {
                "symbol": attr(symbol_node, "text"),
                "name": first_text_near(root, f"{PACKAGE}:id/d7s", row_top, row_bottom),
                "market_value": first_text_near(root, f"{PACKAGE}:id/d6b", row_top, row_bottom),
                "quantity": first_text_near(root, f"{PACKAGE}:id/iuh", row_top, row_bottom),
                "price": first_text_near(root, f"{PACKAGE}:id/d8a", row_top, row_bottom),
                "cost": first_text_near(root, f"{PACKAGE}:id/izx", row_top, row_bottom),
                "daily_pnl": first_text_near(root, f"{PACKAGE}:id/d_y", row_top, row_bottom),
                "daily_pnl_pct": first_text_near(root, f"{PACKAGE}:id/d_p", row_top, row_bottom),
                "position_pnl": first_text_near(root, f"{PACKAGE}:id/d3n", row_top, row_bottom),
                "position_pnl_pct": first_text_near(root, f"{PACKAGE}:id/d3k", row_top, row_bottom),
            }
        )

    return {
        "account_type": account_type,
        "account_number": account_number,
        "net_asset_value": net_asset_value,
        "net_asset_currency": nav_currency,
        "cash_accounts": cash_accounts,
        "positions": positions,
    }


def collect_us_account_snapshot(root: ET.Element) -> dict:
    top_title = ""
    for text, node_bounds in texts_by_id(root, f"{PACKAGE}:id/dh8"):
        _, top, _, _ = node_bounds
        if top < 300:
            top_title = text
            break

    detail_values = texts_by_id(root, f"{PACKAGE}:id/dby")
    detail_values.sort(key=lambda item: item[1][0])

    stock_market_value = detail_values[0][0] if len(detail_values) > 0 else ""
    buying_power = detail_values[1][0] if len(detail_values) > 1 else ""
    withdrawable_cash = detail_values[2][0] if len(detail_values) > 2 else ""

    positions = []
    for symbol_node in root.iter("node"):
        if attr(symbol_node, "resource-id") != f"{PACKAGE}:id/iwb":
            continue
        _, top, _, bottom = bounds(symbol_node)
        row_top = max(0, top - 90)
        row_bottom = min(2424, bottom + 80)
        positions.append(
            {
                "symbol": attr(symbol_node, "text"),
                "name": first_text_near(root, f"{PACKAGE}:id/d7s", row_top, row_bottom),
                "market_value": first_text_near(root, f"{PACKAGE}:id/d6l", row_top, row_bottom)
                or first_text_near(root, f"{PACKAGE}:id/d6b", row_top, row_bottom),
                "quantity": first_text_near(root, f"{PACKAGE}:id/iuh", row_top, row_bottom),
                "price": first_text_near(root, f"{PACKAGE}:id/d8a", row_top, row_bottom),
                "cost": first_text_near(root, f"{PACKAGE}:id/izx", row_top, row_bottom),
                "daily_pnl": first_text_near(root, f"{PACKAGE}:id/d_y", row_top, row_bottom),
                "daily_pnl_pct": first_text_near(root, f"{PACKAGE}:id/d_p", row_top, row_bottom),
                "position_pnl": first_text_near(root, f"{PACKAGE}:id/d3n", row_top, row_bottom),
                "position_pnl_pct": first_text_near(root, f"{PACKAGE}:id/d3k", row_top, row_bottom),
            }
        )

    return {
        "source": "ui_automator",
        "account": top_title or "US Account",
        "currency": "USD",
        "net_liquidation_value": attr(find_first(root, **{"resource-id": f"{PACKAGE}:id/dhr"}) or ET.Element("node"), "text"),
        "position_pnl": attr(find_first(root, **{"resource-id": f"{PACKAGE}:id/d9v"}) or ET.Element("node"), "text"),
        "daily_pnl": first_text_near(root, f"{PACKAGE}:id/dhq", 350, 650),
        "stock_market_value": stock_market_value,
        "buying_power": buying_power,
        "withdrawable_cash": withdrawable_cash,
        "cash_accounts": [
            {
                "account": "USD Cash",
                "value": withdrawable_cash,
            }
        ],
        "positions": positions,
    }


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ocr_words_with_windows(image_path: Path) -> list[dict]:
    ps = f"""
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
function AwaitOp($op, [type]$resultType) {{
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {{ $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 }} |
    Select-Object -First 1
  $task = $method.MakeGenericMethod($resultType).Invoke($null, @($op))
  return $task.GetAwaiter().GetResult()
}}
$path = {powershell_quote(str(image_path.resolve()))}
$file = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = AwaitOp ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = AwaitOp ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = AwaitOp ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {{ throw 'Windows OCR engine is not available.' }}
$result = AwaitOp ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$items = @()
foreach ($line in $result.Lines) {{
  foreach ($word in $line.Words) {{
    $r = $word.BoundingRect
    $items += [pscustomobject]@{{
      text = $word.Text
      x = [double]$r.X
      y = [double]$r.Y
      w = [double]$r.Width
      h = [double]$r.Height
    }}
  }}
}}
$items | ConvertTo-Json -Compress -Depth 3
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AutomationError(f"Windows OCR failed: {(proc.stderr or proc.stdout).strip()}")
    output = proc.stdout.strip()
    if not output:
        return []
    parsed = json.loads(output)
    if isinstance(parsed, dict):
        return [parsed]
    return parsed


def normalized_ocr_number(text: str) -> str:
    cleaned = text.strip().replace("—", "-").replace("−", "-")
    cleaned = cleaned.replace("\u2014", "-").replace("\u2212", "-")
    if "\u00af" in cleaned:
        without_overline = cleaned.replace("\u00af", "").strip()
        if without_overline and not without_overline.startswith("-"):
            cleaned = f"-{without_overline}"
        else:
            cleaned = without_overline
    if cleaned in {"o", "O"}:
        return "0"
    if cleaned.rstrip().endswith(","):
        return ""
    cleaned = re.sub(r"\b[Io]\s+\.(?=\d)", lambda match: f"{'1' if match.group(0)[0] == 'I' else '0'}.", cleaned)
    cleaned = re.sub(r",\s+", ",", cleaned)
    candidate_text = cleaned.replace("I", "1").replace("O", "0").replace("o", "0")
    candidate_text = re.sub(r"(?<=\d)\s+\.(?=\d)", ".", candidate_text)
    candidates = [
        candidate
        for candidate in re.findall(r"-?(?:\d[\d,]*\.?\d*|\.\d+)%?", candidate_text)
        if not candidate.rstrip().endswith(",")
    ]
    if candidates and candidate_text.strip() != cleaned.strip():
        return candidates[0]
    compact = cleaned.replace(" ", "").replace(",", "")
    if compact == "-":
        return ""
    if compact == "--":
        return cleaned
    if re.fullmatch(r"-?\d+(?:\.\d+)?%?", compact):
        return cleaned
    if candidates:
        return candidates[0]
    if cleaned:
        return ""
    return cleaned


def words_in_region(words: list[dict], *, x_min: float = 0, x_max: float = 1080, y_min: float = 0, y_max: float = 2424) -> list[dict]:
    found = []
    for word in words:
        x = float(word.get("x", 0))
        y = float(word.get("y", 0))
        w = float(word.get("w", 0))
        h = float(word.get("h", 0))
        cx = x + w / 2
        cy = y + h / 2
        if x_min <= cx <= x_max and y_min <= cy <= y_max:
            found.append(word)
    return sorted(found, key=lambda item: (float(item.get("y", 0)), float(item.get("x", 0))))


def text_in_region(words: list[dict], *, x_min: float = 0, x_max: float = 1080, y_min: float = 0, y_max: float = 2424) -> str:
    return " ".join(str(word.get("text", "")) for word in words_in_region(words, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)).strip()


def number_in_region(words: list[dict], *, x_min: float, x_max: float, y_min: float, y_max: float) -> str:
    text = text_in_region(words, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)
    return normalized_ocr_number(text)


def first_word_center_y(words: list[dict], prefix: str, *, x_min: float, x_max: float, y_min: float, y_max: float) -> float | None:
    prefix_lower = prefix.lower()
    for word in words_in_region(words, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max):
        if str(word.get("text", "")).lower().startswith(prefix_lower):
            return float(word["y"]) + float(word["h"]) / 2
    return None


def first_word_center(words: list[dict], prefix: str, *, x_min: float, x_max: float, y_min: float, y_max: float) -> tuple[int, int] | None:
    prefix_lower = prefix.lower()
    for word in words_in_region(words, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max):
        if str(word.get("text", "")).lower().startswith(prefix_lower):
            x = float(word["x"]) + float(word["w"]) / 2
            y = float(word["y"]) + float(word["h"]) / 2
            return int(x), int(y)
    return None


def find_table_header_y(words: list[dict]) -> float:
    header_words = words_in_region(words, x_min=0, x_max=1080, y_min=700, y_max=1800)
    for word in header_words:
        if str(word.get("text", "")).lower().startswith("symbol"):
            return float(word["y"]) + float(word["h"]) / 2
    return 450


def find_position_symbols(words: list[dict]) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    header_y = find_table_header_y(words)
    left_words = words_in_region(words, x_min=0, x_max=300, y_min=header_y + 35, y_max=2150)
    by_line: list[list[dict]] = []
    for word in left_words:
        cy = float(word["y"]) + float(word["h"]) / 2
        for line in by_line:
            line_cy = sum(float(item["y"]) + float(item["h"]) / 2 for item in line) / len(line)
            if abs(cy - line_cy) <= 18:
                line.append(word)
                break
        else:
            by_line.append([word])

    for line in by_line:
        line.sort(key=lambda item: float(item["x"]))
        text = "".join(str(item["text"]) for item in line)
        normalized = text.replace(" ", "")
        match = re.search(r"([A-Za-z0-9]+\.US)", normalized)
        if match is None:
            continue
        symbol = match.group(1).upper()
        cy = sum(float(item["y"]) + float(item["h"]) / 2 for item in line) / len(line)
        rows.append((symbol, cy))
    return rows


def parse_position_left_rows(words: list[dict]) -> list[dict]:
    positions = []
    for symbol, symbol_y in find_position_symbols(words):
        value_y = symbol_y - 65
        name = text_in_region(words, x_min=40, x_max=290, y_min=value_y - 32, y_max=value_y + 32)
        market_value = number_in_region(words, x_min=250, x_max=470, y_min=value_y - 35, y_max=value_y + 35)
        quantity = number_in_region(words, x_min=250, x_max=470, y_min=symbol_y - 30, y_max=symbol_y + 30)
        if not quantity and market_value in {"0", "0.00"}:
            quantity = "0"
        positions.append(
            {
                "symbol": symbol,
                "name": name,
                "market_value": market_value,
                "quantity": quantity,
                "price": number_in_region(words, x_min=430, x_max=650, y_min=value_y - 35, y_max=value_y + 35),
                "cost": number_in_region(words, x_min=430, x_max=650, y_min=symbol_y - 30, y_max=symbol_y + 30),
                "daily_pnl": number_in_region(words, x_min=590, x_max=860, y_min=value_y - 35, y_max=value_y + 35),
                "daily_pnl_pct": number_in_region(words, x_min=590, x_max=860, y_min=symbol_y - 30, y_max=symbol_y + 30),
                "position_pnl": "",
                "position_pnl_pct": "",
                "_symbol_y": symbol_y,
            }
        )
    return positions


def find_position_pnl_x_min(words: list[dict]) -> float | None:
    header_words = words_in_region(words, x_min=300, x_max=1080, y_min=900, y_max=1500)
    for word in header_words:
        text = str(word.get("text", "")).lower()
        if text.startswith("posit"):
            return max(300, float(word["x"]) - 30)
    return None


def parse_position_right_fields(words: list[dict], left_rows: list[dict], *, x_min: float | None = None) -> dict[str, dict]:
    position_pnl_x_min = x_min if x_min is not None else find_position_pnl_x_min(words)
    if position_pnl_x_min is None:
        return {}

    parsed: dict[str, dict] = {}

    right_symbols = {symbol: y for symbol, y in find_position_symbols(words)}
    for left in left_rows:
        symbol = left["symbol"]
        symbol_y = right_symbols.get(symbol, left.get("_symbol_y", 0))
        value_y = float(symbol_y) - 65
        parsed[symbol] = {
            "position_pnl": number_in_region(words, x_min=position_pnl_x_min, x_max=1080, y_min=value_y - 35, y_max=value_y + 35),
            "position_pnl_pct": number_in_region(words, x_min=position_pnl_x_min, x_max=1080, y_min=float(symbol_y) - 30, y_max=float(symbol_y) + 30),
        }
    return parsed


def merge_position(existing: dict, update: dict, *, prefer_update: bool = False) -> dict:
    merged = dict(existing)
    for key, value in update.items():
        if key.startswith("_"):
            continue
        if value and (prefer_update or not merged.get(key)):
            merged[key] = value
        elif value and key in {"position_pnl", "position_pnl_pct"}:
            merged[key] = value
    return merged


def collect_holdings_from_account_screen(words: list[dict]) -> dict:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    if "Open Acc" in all_text or "Speed Account Opening" in all_text:
        raise AutomationError("MetaStock is showing Open Acc instead of holdings. Log in/link the trading account before querying holdings.")
    if "Account" not in all_text or "US" not in all_text:
        raise AutomationError(f"Account screen OCR did not find holdings text: {all_text[:300]}")

    account = text_in_region(words, x_min=130, x_max=620, y_min=280, y_max=370)
    currency = text_in_region(words, x_min=850, x_max=1000, y_min=390, y_max=470) or "HKD"

    us_account_y = first_word_center_y(words, "US", x_min=80, x_max=360, y_min=800, y_max=1800) or 1050
    hk_account_y = first_word_center_y(words, "HK", x_min=80, x_max=360, y_min=1500, y_max=2300) or 2050
    us_value = number_in_region(words, x_min=40, x_max=390, y_min=us_account_y + 55, y_max=us_account_y + 170)
    us_daily_pnl = number_in_region(words, x_min=620, x_max=830, y_min=us_account_y + 95, y_max=us_account_y + 190)
    us_total_pnl = number_in_region(words, x_min=840, x_max=1040, y_min=us_account_y + 95, y_max=us_account_y + 190)
    hk_value = number_in_region(words, x_min=40, x_max=250, y_min=hk_account_y + 55, y_max=hk_account_y + 170)
    hk_daily_pnl = number_in_region(words, x_min=620, x_max=850, y_min=hk_account_y + 95, y_max=hk_account_y + 190)
    hk_total_pnl = number_in_region(words, x_min=850, x_max=1040, y_min=hk_account_y + 95, y_max=hk_account_y + 190)

    positions = parse_position_left_rows(words)

    return {
        "source": "account_screen_ocr",
        "account": account or "Account",
        "currency": currency,
        "net_liquidation_value": "",
        "stock_market_value": us_value,
        "buying_power": "",
        "withdrawable_cash": "",
        "daily_pnl": us_daily_pnl,
        "position_pnl": us_total_pnl,
        "cash_accounts": [
            {"account": "US Account(USD)", "value": us_value, "daily_pnl": us_daily_pnl, "total_pnl": us_total_pnl},
            {"account": "HK Account(HKD)", "value": hk_value, "daily_pnl": hk_daily_pnl, "total_pnl": hk_total_pnl},
        ],
        "positions": positions,
    }


def collect_account_detail_from_screen(words: list[dict]) -> dict:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    has_buying_power_label = "Buying" in all_text and "Power" in all_text
    has_detail_label = "Liquidation" in all_text and ("US Account" in all_text or "HK Account" in all_text)
    if "Account" not in all_text or not (has_buying_power_label or has_detail_label):
        return {}

    currency = "USD" if "US Account" in all_text or "(USD)" in all_text else "HKD"
    return {
        "source": "account_detail_ocr",
        "account": "US Account" if currency == "USD" else "HK Account",
        "currency": currency,
        "net_liquidation_value": number_in_region(words, x_min=55, x_max=530, y_min=400, y_max=540),
        "position_pnl": number_in_region(words, x_min=830, x_max=1040, y_min=400, y_max=465),
        "daily_pnl": number_in_region(words, x_min=830, x_max=1040, y_min=465, y_max=535),
        "stock_market_value": number_in_region(words, x_min=50, x_max=260, y_min=620, y_max=690),
        "buying_power": number_in_region(words, x_min=360, x_max=610, y_min=620, y_max=690),
        "withdrawable_cash": number_in_region(words, x_min=680, x_max=860, y_min=620, y_max=690),
    }


def merge_account_detail(snapshot: dict, detail: dict) -> None:
    if not detail:
        return

    for key in (
        "currency",
        "net_liquidation_value",
        "stock_market_value",
        "buying_power",
        "withdrawable_cash",
        "daily_pnl",
        "position_pnl",
    ):
        value = detail.get(key)
        if value:
            snapshot[key] = value

    for item in snapshot.get("cash_accounts", []):
        if str(item.get("account", "")).startswith(detail.get("account", "")):
            for key in (
                "net_liquidation_value",
                "stock_market_value",
                "buying_power",
                "withdrawable_cash",
            ):
                item[key] = detail.get(key, "")
            break


def raise_if_ocr_app_problem(words: list[dict]) -> None:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    lower_text = all_text.lower()
    crash_markers = [
        "keeps stopping",
        "has stopped",
        "isn't responding",
        "is not responding",
        "close app",
        "app info",
        "wait",
    ]
    if "metastock" in lower_text and any(marker in lower_text for marker in crash_markers):
        raise AutomationError(f"MetaStock appears to have crashed or stopped responding: {all_text[:300]}")

    login_markers = [
        "login/register",
        "log in",
        "login",
        "sign in",
        "please login",
        "please log in",
    ]
    if any(marker in lower_text for marker in login_markers):
        raise AutomationError(f"MetaStock trading account is not logged in: {all_text[:300]}")

    if "Open Acc" in all_text or "Speed Account Opening" in all_text:
        raise AutomationError("MetaStock is showing Open Acc instead of holdings. Log in/link the trading account before querying holdings.")


def raise_if_not_holdings_screen(words: list[dict]) -> None:
    if not is_holdings_screen(words):
        all_text = " ".join(str(word.get("text", "")) for word in words)
        raise AutomationError(f"MetaStock Account holdings screen is not visible: {all_text[:300]}")


def is_holdings_screen(words: list[dict]) -> bool:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    holdings_markers = ["Net Asset Value", "US Account", "HK Account", "Position", "Today's Order"]
    return (
        "Account" in all_text
        and any(marker in all_text for marker in holdings_markers)
        and "Watchlist" in all_text
        and "Me" in all_text
    )


def declared_position_count(words: list[dict]) -> int | None:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    match = re.search(r"Position\s*\((\d+)\)", all_text)
    if match:
        return int(match.group(1))
    return None


def has_position_action_overlay(words: list[dict]) -> bool:
    visible = {str(word.get("text", "")) for word in words}
    return {"Quote", "Buy", "Sell", "Share"}.issubset(visible)


def capture_raw_ocr_words(adb: str) -> list[dict]:
    capture_screenshot(adb, HOLDINGS_SCREENSHOT)
    return ocr_words_with_windows(HOLDINGS_SCREENSHOT)


def is_account_detail_screen(words: list[dict]) -> bool:
    all_text = " ".join(str(word.get("text", "")) for word in words)
    if "Watchlist" in all_text or "Me" in all_text:
        return False
    account_title = "HK Account" in all_text or "US Account" in all_text
    return account_title and "Net Liquidation Value" in all_text and "Positions" in all_text


def return_to_holdings_from_account_detail(adb: str) -> list[dict]:
    last_words: list[dict] = []
    for _ in range(4):
        if foreground_package(adb) != PACKAGE:
            launch_metastock_for_ocr(adb)
            time.sleep(1.0)

        last_words = capture_raw_ocr_words(adb)
        if is_holdings_screen(last_words) and not is_account_detail_screen(last_words):
            return last_words

        all_text = " ".join(str(word.get("text", "")) for word in last_words)
        if is_account_detail_screen(last_words) or ("Account" in all_text and "Liquidation" in all_text):
            tap_xy(adb, 72, 190)
        else:
            tap_xy(adb, 756, 2295)
        time.sleep(1.0)

    return last_words


def leave_account_detail_if_visible(adb: str) -> bool:
    for _ in range(4):
        words = capture_raw_ocr_words(adb)
        if not is_account_detail_screen(words):
            time.sleep(0.25)
            continue

        words = return_to_holdings_from_account_detail(adb)
        return not is_account_detail_screen(words)
    return False


def dismiss_position_action_overlay(adb: str) -> None:
    shell(adb, "input", "keyevent", "BACK")
    time.sleep(0.5)


def capture_holdings_words(adb: str) -> list[dict]:
    for _ in range(3):
        words = capture_raw_ocr_words(adb)
        if not has_position_action_overlay(words):
            raise_if_ocr_app_problem(words)
            raise_if_not_holdings_screen(words)
            return words
        dismiss_position_action_overlay(adb)
    raise AutomationError("MetaStock position action overlay is still visible and blocking holdings OCR.")


def table_horizontal_swipe_y(words: list[dict]) -> int:
    symbols = find_position_symbols(words)
    if symbols:
        return int(min(2050, max(1200, symbols[0][1])))
    header_y = find_table_header_y(words)
    return int(min(1800, max(1200, header_y + 80)))


def reveal_position_pnl_columns(adb: str, words: list[dict]) -> None:
    y = table_horizontal_swipe_y(words)
    swipe_xy(adb, 980, y, 260, y, 80)


def is_today_order_table_visible(words: list[dict]) -> bool:
    header_text = text_in_region(words, x_min=250, x_max=1080, y_min=1300, y_max=1450)
    body_text = text_in_region(words, x_min=250, x_max=1080, y_min=1450, y_max=1800)
    return "Order No" in header_text or "Status" in header_text or "Completed" in body_text


def select_position_table(adb: str, words: list[dict]) -> list[dict]:
    if not is_today_order_table_visible(words):
        return words

    position_tab = first_word_center(words, "Position", x_min=80, x_max=360, y_min=1150, y_max=1350)
    if position_tab is None:
        return words

    tap_xy(adb, *position_tab)
    time.sleep(0.8)
    return capture_holdings_words(adb)


def scroll_account_to_top(adb: str) -> None:
    swipe_xy(adb, 540, 900, 540, 1850, 500)


def reset_position_table_layout(adb: str) -> None:
    swipe_xy(adb, 540, 1800, 540, 1200, 400)
    time.sleep(0.5)
    swipe_xy(adb, 540, 1200, 540, 1800, 400)
    time.sleep(0.5)


def scroll_positions_down(adb: str) -> None:
    swipe_xy(adb, 540, 1880, 540, 1250, 450)


def strip_internal_position_fields(position: dict) -> dict:
    return {key: value for key, value in position.items() if not key.startswith("_")}


def merge_visible_positions(
    positions_by_symbol: dict[str, dict],
    left_rows: list[dict],
    right_fields_by_symbol: dict[str, dict],
) -> None:
    for row in left_rows:
        symbol = row["symbol"]
        merged = merge_position(row, right_fields_by_symbol.get(symbol, {}))
        if symbol in positions_by_symbol:
            positions_by_symbol[symbol] = merge_position(positions_by_symbol[symbol], merged)
        else:
            positions_by_symbol[symbol] = merged


def position_fields_have_values(right_fields_by_symbol: dict[str, dict]) -> bool:
    for fields in right_fields_by_symbol.values():
        value = fields.get("position_pnl", "")
        compact = str(value).replace(",", "").replace(" ", "")
        if re.fullmatch(r"-?\d+\.\d+", compact):
            return True
    return False


class QueryHoldingsFlow(MetaStockFlow):
    def open_account_words(self) -> list[dict]:
        last_error: AutomationError | None = None
        for attempt in range(3):
            launch_metastock_for_ocr(self.adb)
            leave_account_detail_if_visible(self.adb)
            tap_xy(self.adb, 756, 2295)
            time.sleep(3.0)
            if leave_account_detail_if_visible(self.adb):
                tap_xy(self.adb, 756, 2295)
                time.sleep(3.0)
            for _ in range(2):
                scroll_account_to_top(self.adb)
                time.sleep(0.2)
            reset_position_table_layout(self.adb)
            try:
                words = capture_holdings_words(self.adb)
                words = select_position_table(self.adb, words)
                collect_holdings_from_account_screen(words)
                return words
            except AutomationError as exc:
                last_error = exc
                if attempt == 2:
                    raise
                launch_metastock_for_ocr(self.adb)
                time.sleep(0.5)
        raise last_error or AutomationError("Could not read holdings from the Account screen.")

    def collect_us_account_detail(self, main_words: list[dict]) -> dict:
        words = main_words
        for _ in range(2):
            us_account_y = first_word_center_y(words, "US", x_min=80, x_max=360, y_min=800, y_max=1800)
            if us_account_y is None:
                time.sleep(0.6)
                words = capture_holdings_words(self.adb)
                continue

            tap_xy(self.adb, 180, int(us_account_y + 35))
            time.sleep(1.6)
            detail_words = capture_raw_ocr_words(self.adb)
            detail = collect_account_detail_from_screen(detail_words)
            detail_text = " ".join(str(word.get("text", "")) for word in detail_words)
            opened_detail = is_account_detail_screen(detail_words) or (
                "Account" in detail_text and "Net" in detail_text and "Liquidation" in detail_text
            )
            if detail or opened_detail:
                words = return_to_holdings_from_account_detail(self.adb)
                if detail:
                    return detail
                continue

            if has_position_action_overlay(detail_words):
                dismiss_position_action_overlay(self.adb)
                words = capture_holdings_words(self.adb)
                continue
            words = detail_words
        return {}

    def query(self, *, max_pages: int = 20) -> dict:
        if max_pages <= 0:
            raise AutomationError("--holdings-max-pages must be greater than zero.")

        first_words = self.open_account_words()
        detail = self.collect_us_account_detail(first_words)
        first_words = capture_holdings_words(self.adb)
        first_words = select_position_table(self.adb, first_words)
        snapshot = collect_holdings_from_account_screen(first_words)
        merge_account_detail(snapshot, detail)
        positions_by_symbol: dict[str, dict] = {}
        seen_pages: set[str] = set()
        position_pnl_x_min: float | None = None
        expected_position_count = declared_position_count(first_words)
        left_words = first_words

        for page_index in range(max_pages):
            left_rows = snapshot["positions"] if page_index == 0 else parse_position_left_rows(left_words)
            page_signature = "|".join(row["symbol"] for row in left_rows)
            if not left_rows or page_signature in seen_pages:
                break
            seen_pages.add(page_signature)

            detected_x_min = find_position_pnl_x_min(left_words)
            if detected_x_min is not None:
                position_pnl_x_min = detected_x_min
                right_fields_by_symbol = parse_position_right_fields(
                    left_words,
                    left_rows,
                    x_min=position_pnl_x_min,
                )
            else:
                right_fields_by_symbol = {}

            if not position_fields_have_values(right_fields_by_symbol):
                for _ in range(2):
                    reveal_position_pnl_columns(self.adb, left_words)
                    time.sleep(0.5)
                    right_words = capture_holdings_words(self.adb)
                    right_left_rows_by_symbol = {
                        row["symbol"]: row for row in parse_position_left_rows(right_words)
                    }
                    left_rows = [
                        merge_position(row, right_left_rows_by_symbol.get(row["symbol"], {}), prefer_update=True)
                        for row in left_rows
                    ]
                    detected_x_min = find_position_pnl_x_min(right_words)
                    if detected_x_min is not None:
                        position_pnl_x_min = detected_x_min
                    right_fields_by_symbol = parse_position_right_fields(
                        right_words,
                        left_rows,
                        x_min=position_pnl_x_min,
                    )
                    if position_fields_have_values(right_fields_by_symbol):
                        break

            merge_visible_positions(positions_by_symbol, left_rows, right_fields_by_symbol)

            if expected_position_count is not None and len(positions_by_symbol) >= expected_position_count:
                break
            if page_index == max_pages - 1:
                break
            scroll_positions_down(self.adb)
            time.sleep(0.8)
            left_words = capture_holdings_words(self.adb)

        snapshot["source"] = "account_screen_ocr_scroll"
        snapshot["positions"] = [strip_internal_position_fields(position) for position in positions_by_symbol.values()]
        return snapshot


def open_account_ocr_words(adb: str) -> list[dict]:
    return QueryHoldingsFlow(adb).open_account_words()


def query_holdings(adb: str, *, max_pages: int = 20) -> dict:
    return QueryHoldingsFlow(adb).query(max_pages=max_pages)


def print_holdings(snapshot: dict) -> None:
    if snapshot.get("source") == "windows_ocr":
        print("Holdings OCR fallback:")
        for line in snapshot["ocr_lines"]:
            print(line)
        return

    print(f"Account: {snapshot['account']}")
    print(f"Net Liquidation Value: {snapshot['net_liquidation_value']} {snapshot['currency']}")
    print(f"Stock Market Value: {snapshot['stock_market_value']} {snapshot['currency']}")
    print(f"Buying Power: {snapshot['buying_power']} {snapshot['currency']}")
    print(f"Withdrawable Cash: {snapshot['withdrawable_cash']} {snapshot['currency']}")
    print(f"Daily P&L: {snapshot['daily_pnl']}")
    print(f"Position P&L: {snapshot['position_pnl']}")

    print("\nCash:")
    for item in snapshot["cash_accounts"]:
        print(f"- {item['account']}: value={item['value']}")

    print("\nStock Positions:")
    if not snapshot["positions"]:
        print("- No positions")
        return
    for item in snapshot["positions"]:
        print(
            f"- {item['symbol']} {item['name']}: qty={item['quantity']} "
            f"value={item['market_value']} price={item['price']} cost={item['cost']} "
            f"daily_pnl={item['daily_pnl']} ({item['daily_pnl_pct']}) "
            f"position_pnl={item['position_pnl']} ({item['position_pnl_pct']})"
        )


@dataclass(frozen=True)
class OrderRow:
    symbol: str
    name: str
    price: str
    quantity: str
    status: str
    side: str
    tap_y: int

    @property
    def signature(self) -> str:
        return "|".join([self.symbol, self.name, self.price, self.quantity, self.status, self.side, str(self.tap_y)])


def open_order_page(adb: str) -> ET.Element:
    launch_metastock(adb)
    root = dump_ui(adb)
    root = dismiss_order_overlay_if_present(adb, root)
    if has_order_page(root):
        return root
    if has_trade_ticket(root):
        for _ in range(3):
            shell(adb, "input", "keyevent", "BACK")
            time.sleep(0.8)
            root = dump_ui(adb)
            if not has_trade_ticket(root):
                break

    account = find_by_text(root, "Account")
    if account is None:
        account_markers = ["Net Asset Value", "US Account(USD)", "HK Account(HKD)"]
        if any(find_by_text(root, marker) is not None for marker in account_markers):
            tap_xy(adb, 334, 916)
            return wait_for_ui(adb, has_order_page, timeout=8.0)
        if find_by_text(root, "Login/Register") is not None:
            raise AutomationError("MetaStock is showing Login/Register; log in before cancelling orders.")
        tap_xy(adb, 756, 2295)
        time.sleep(1.0)
        tap_xy(adb, 334, 916)
        root = wait_for_ui(adb, has_order_page, timeout=8.0)
        if has_order_page(root):
            return root
        raise AutomationError("Could not find the Account tab.")

    tap(adb, account)

    def account_page_visible(r: ET.Element) -> bool:
        return find_by_text(r, "Net Asset Value") is not None or find_by_text(r, "US Account(USD)") is not None

    root = wait_for_ui(adb, account_page_visible, timeout=8.0)
    order_shortcuts = []
    for node in find_all_by_text(root, "Order"):
        _, top, _, bottom = bounds(node)
        if (top + bottom) // 2 > 500:
            order_shortcuts.append(node)
    if not order_shortcuts:
        raise AutomationError("Could not find the Account > Order shortcut.")

    tap(adb, order_shortcuts[0])
    return wait_for_ui(adb, has_order_page, timeout=8.0)


def select_pending_queue_filter(adb: str, root: ET.Element) -> ET.Element:
    status_filter = find_first(root, **{"resource-id": f"{PACKAGE}:id/djv"})
    if status_filter is not None and attr(status_filter, "text") == "Pending Queue":
        return root
    sheet_already_open = find_by_text(root, "Order Status") is not None and find_by_text(root, "Pending Queue") is not None
    if status_filter is None and not sheet_already_open:
        raise AutomationError("Could not find the order status filter.")

    if not sheet_already_open:
        tap(adb, status_filter)

    def status_sheet_visible(r: ET.Element) -> bool:
        return find_by_text(r, "Order Status") is not None and find_by_text(r, "Pending Queue") is not None

    root = wait_for_ui(adb, status_sheet_visible, timeout=5.0)
    pending = find_by_text(root, "Pending Queue")
    confirm = find_by_text(root, "Confirm")
    if pending is not None:
        tap(adb, pending)
    else:
        tap_xy(adb, 188, 773)
    time.sleep(0.2)
    if confirm is not None:
        tap(adb, confirm)
    else:
        tap_xy(adb, 800, 1078)

    return wait_for_ui(
        adb,
        lambda r: (find_first(r, **{"resource-id": f"{PACKAGE}:id/djv"}) is not None
                   and attr(find_first(r, **{"resource-id": f"{PACKAGE}:id/djv"}), "text") == "Pending Queue"),
        timeout=8.0,
    )


def node_text_near(root: ET.Element, resource_id: str, top: int, bottom: int) -> str:
    for node in root.iter("node"):
        if attr(node, "resource-id") != resource_id:
            continue
        _, node_top, _, node_bottom = bounds(node)
        node_center = (node_top + node_bottom) // 2
        if top <= node_center <= bottom:
            return attr(node, "text")
    return ""


def visible_order_rows(root: ET.Element) -> list[OrderRow]:
    rows: list[OrderRow] = []
    symbol_id = f"{PACKAGE}:id/d6s"
    name_id = f"{PACKAGE}:id/d7s"
    price_id = f"{PACKAGE}:id/d_9"
    qty_id = f"{PACKAGE}:id/ius"
    status_id = f"{PACKAGE}:id/dsm"
    side_id = f"{PACKAGE}:id/dsh"

    for symbol_node in root.iter("node"):
        if attr(symbol_node, "resource-id") != symbol_id:
            continue

        left, top, right, bottom = bounds(symbol_node)
        row_top = max(0, top - 90)
        row_bottom = min(2424, bottom + 80)
        symbol = attr(symbol_node, "text").upper()
        name = node_text_near(root, name_id, row_top, row_bottom)
        price = node_text_near(root, price_id, row_top, row_bottom)
        quantity = node_text_near(root, qty_id, row_top, row_bottom)
        status = node_text_near(root, status_id, row_top, row_bottom)
        side = node_text_near(root, side_id, row_top, row_bottom)
        rows.append(
            OrderRow(
                symbol=symbol,
                name=name,
                price=price,
                quantity=quantity,
                status=status,
                side=side,
                tap_y=(row_top + row_bottom) // 2,
            )
        )
        _ = left, right
    return rows


def scroll_order_list(adb: str) -> None:
    shell(adb, "input", "swipe", "540", "2010", "540", "650", "650")
    time.sleep(0.8)


def find_cancel_control(root: ET.Element) -> ET.Element | None:
    labels = ["Cancel Order", "Cancel order", "Cancel", "撤单", "撤單"]
    for node in root.iter("node"):
        if attr(node, "text") not in labels or attr(node, "clickable") != "true":
            continue
        _, top, _, _ = bounds(node)
        if top > 350:
            return node
    return None


def tap_confirmation_if_present(adb: str, timeout: float = 5.0) -> bool:
    confirm_labels = ["Confirm", "OK", "Yes", "确定", "確認"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        root = dump_ui(adb)
        for label in confirm_labels:
            node = find_by_text(root, label)
            if node is not None and attr(node, "clickable") == "true":
                tap(adb, node)
                return True
        time.sleep(0.3)
    return False


def cancel_one_order_row(adb: str, row: OrderRow) -> None:
    tap_xy(adb, 540, row.tap_y)

    def cancel_visible(r: ET.Element) -> bool:
        return find_cancel_control(r) is not None

    root = wait_for_ui(adb, cancel_visible, timeout=8.0)
    cancel = find_cancel_control(root)
    if cancel is None:
        raise AutomationError(f"Opened {row.symbol}, but no cancel control was found.")
    tap(adb, cancel)
    tap_confirmation_if_present(adb)
    time.sleep(1.0)


class CancelOrdersFlow(MetaStockFlow):
    def cancel_pending(self, *, execute: bool, symbol: str | None = None) -> int:
        expected_symbol = normalize_symbol(symbol)[1] if symbol else None
        target_label = expected_symbol if expected_symbol else "all symbols"
        root = open_order_page(self.adb)
        root = select_pending_queue_filter(self.adb, root)

        seen_pages: set[str] = set()
        matched: list[OrderRow] = []
        cancelled = 0

        while True:
            root = dump_ui(self.adb)
            rows = visible_order_rows(root)
            matching_rows = [row for row in rows if expected_symbol is None or row.symbol == expected_symbol]

            if execute and matching_rows:
                row = matching_rows[0]
                print(f"Cancelling pending order: {row.side} {row.symbol} qty={row.quantity} price={row.price}")
                cancel_one_order_row(self.adb, row)
                cancelled += 1
                root = open_order_page(self.adb)
                select_pending_queue_filter(self.adb, root)
                continue

            matched.extend(row for row in matching_rows if row.signature not in {known.signature for known in matched})

            page_signature = "|".join(row.signature for row in rows)
            if not rows or page_signature in seen_pages:
                break
            seen_pages.add(page_signature)
            scroll_order_list(self.adb)

        if not execute:
            if matched:
                print(f"Dry run: found {len(matched)} pending order(s) for {target_label}:")
                for row in matched:
                    print(f"- {row.side} {row.symbol} qty={row.quantity} price={row.price} status={row.status}")
                print("Re-run with --execute-cancel to cancel them.")
            else:
                print(f"No pending orders found for {target_label}.")
            return len(matched)

        print(f"Cancelled {cancelled} pending order(s) for {target_label}.")
        return cancelled

    def cancel_symbol(self, symbol: str, *, execute: bool) -> int:
        return self.cancel_pending(execute=execute, symbol=symbol)

    def cancel_all(self, *, execute: bool) -> int:
        return self.cancel_pending(execute=execute, symbol=None)


def cancel_pending_orders(adb: str, *, execute: bool, symbol: str | None = None) -> int:
    return CancelOrdersFlow(adb).cancel_pending(execute=execute, symbol=symbol)


def cancel_open_orders(adb: str, symbol: str, *, execute: bool) -> int:
    return CancelOrdersFlow(adb).cancel_symbol(symbol, execute=execute)


def cancel_all_open_orders(adb: str, *, execute: bool) -> int:
    return CancelOrdersFlow(adb).cancel_all(execute=execute)


def positive_decimal(value: str, label: str) -> str:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be numeric") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be greater than zero")
    return value


def positive_int(value: str) -> str:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("quantity must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("quantity must be greater than zero")
    return str(parsed)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill/submit MetaStock limit orders, or cancel pending orders, through the Android emulator.",
    )
    parser.add_argument("symbol", nargs="?", help="Security symbol, for example AAPL or AAPL.US.")
    parser.add_argument("side", nargs="?", choices=["BUY", "SELL", "buy", "sell"], help="Order side.")
    parser.add_argument("price", nargs="?", type=lambda v: positive_decimal(v, "price"), help="Limit price.")
    parser.add_argument("quantity", nargs="?", type=positive_int, help="Share quantity.")
    parser.add_argument("--adb", help="Path to adb.exe if it is not on PATH.")
    parser.add_argument("--submit", action="store_true", help="Tap the live Buy Now/Sell Now button after verification.")
    parser.add_argument("--query-holdings", action="store_true", help="Print cash/account balances and stock positions.")
    parser.add_argument(
        "--holdings-max-pages",
        type=int,
        default=20,
        help="With --query-holdings, maximum vertical position pages to scan. Default: 20.",
    )
    parser.add_argument("--json", action="store_true", help="With --query-holdings, print machine-readable JSON.")
    parser.add_argument("--cancel-open-orders", metavar="SYMBOL", help="Cancel all Pending Queue orders for this symbol.")
    parser.add_argument("--cancel-all-open-orders", action="store_true", help="Cancel all Pending Queue orders for every symbol.")
    parser.add_argument(
        "--execute-cancel",
        action="store_true",
        help="With --cancel-open-orders or --cancel-all-open-orders, actually tap cancel controls. Without this, only list matching orders.",
    )
    parser.add_argument(
        "--confirm-dialog",
        action="store_true",
        help="Deprecated compatibility flag. --submit now always waits for and taps the MetaStock confirmation.",
    )
    parser.add_argument(
        "--confirm-timeout",
        type=float,
        default=4.0,
        help="Seconds to wait for the MetaStock confirmation dialog after tapping Buy Now/Sell Now.",
    )
    parser.add_argument(
        "--result-timeout",
        type=float,
        default=8.0,
        help="Seconds to watch for MetaStock success or failure text after confirming the order.",
    )
    parser.add_argument(
        "--trading-password",
        help="Trading password to enter if MetaStock prompts before first submit. Prefer the environment variable for automation.",
    )
    parser.add_argument(
        "--trading-password-env",
        default="METASTOCK_TRADING_PASSWORD",
        help="Environment variable containing the trading password. Default: METASTOCK_TRADING_PASSWORD.",
    )
    parser.add_argument(
        "--prompt-trading-password",
        action="store_true",
        help="Prompt interactively for the trading password if MetaStock asks and no password was provided.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    adb = find_adb(args.adb)
    ensure_device(adb)

    if args.query_holdings:
        snapshot = QueryHoldingsFlow(adb).query(max_pages=args.holdings_max_pages)
        if args.json:
            print(json.dumps(snapshot, indent=2))
        else:
            print_holdings(snapshot)
        return 0

    if args.cancel_open_orders:
        CancelOrdersFlow(adb).cancel_symbol(args.cancel_open_orders, execute=args.execute_cancel)
        return 0

    if args.cancel_all_open_orders:
        CancelOrdersFlow(adb).cancel_all(execute=args.execute_cancel)
        return 0

    missing = [name for name in ("symbol", "side", "price", "quantity") if getattr(args, name) is None]
    if missing:
        raise AutomationError(
            "Order mode requires symbol, side, price, and quantity. "
            "For cancellation use --cancel-open-orders SYMBOL. "
            "To cancel every pending order use --cancel-all-open-orders. "
            "For holdings use --query-holdings."
        )

    SubmitNewOrderFlow(adb).run(
        args.symbol,
        args.side,
        args.price,
        args.quantity,
        submit=args.submit,
        confirm_timeout=args.confirm_timeout,
        result_timeout=args.result_timeout,
        trading_password=args.trading_password or os.environ.get(args.trading_password_env),
        trading_password_env=args.trading_password_env,
        prompt_trading_password=args.prompt_trading_password,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
