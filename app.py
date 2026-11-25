"""To Do List 앱 (Streamlit).

할 일 추가/수정/삭제, 완료 토글, 필터, 진행률, JSON 파일 영구 저장.

설계 메모
- Streamlit은 위젯을 조작할 때마다 스크립트 전체를 다시 실행한다.
  모든 상태 변경은 위젯 콜백(on_click / on_change)에서 처리한다.
  콜백은 화면을 다시 그리기 "전에" 실행되므로 변경이 즉시 반영되고 중복 처리되지 않는다.
- 파일(todos.json)이 유일한 원본이다. 여러 탭·여러 사용자가 동시에 써도 서로의 변경을
  덮어쓰지 않도록, 모든 변경은 mutate()에서 "최신 파일 읽기 → 변경 하나 적용 → 저장"으로
  처리한다. 화면을 그릴 때도 파일이 바뀌었으면 다시 불러온다(sync_from_disk).
"""

import html
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import streamlit as st

# ── 상수 ────────────────────────────────────────────────
DATA_FILE = Path(__file__).parent / "todos.json"
CATEGORIES = ["업무", "개인", "기타"]
TITLE_MAX_LEN = 100
DATA_VERSION = 1
REQUIRED_FIELDS = {"id", "title", "category", "done", "created_at", "updated_at"}
PAGE_SIZE = 50                # 한 번에 그리는 할 일 개수 (많을 때 속도 유지)
DOUBLE_SUBMIT_SEC = 1.0       # 이 시간 안에 같은 내용이 다시 제출되면 더블클릭으로 보고 무시

CATEGORY_FILTERS = ["전체", *CATEGORIES]
STATUS_FILTERS = ["전체", "미완료", "완료"]

# 카테고리 배지 색 (PRD 5장: 업무 파랑 / 개인 초록 / 기타 회색)
CATEGORY_COLORS = {"업무": "blue", "개인": "green", "기타": "gray"}
CATEGORY_ICONS = {"업무": "💼", "개인": "🏠", "기타": "📌"}

MISSING_MSG = "이미 삭제된 할 일이에요. (다른 탭에서 삭제됐을 수 있어요)"

# Streamlit 기본 색/테마 변수만 쓰므로 라이트/다크 테마 모두에서 그대로 보인다.
CUSTOM_CSS = """
<style>
/* 할 일 한 줄: 모바일(폭 640px 미만)에서도 세로로 쌓이지 않고 한 줄 유지 */
[class*="st-key-row_"] [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    gap: 0.5rem;
}
[class*="st-key-row_"] [data-testid="stColumn"] {
    min-width: 0 !important;
}
[class*="st-key-row_"] [data-testid="stColumn"]:has([data-testid="stCheckbox"]),
[class*="st-key-row_"] [data-testid="stColumn"]:has([data-testid="stButton"]),
[class*="st-key-row_"] [data-testid="stColumn"]:has([data-testid="stFormSubmitButton"]),
[class*="st-key-row_"] [data-testid="stColumn"]:has([class*="st-key-badge_"]) {
    flex: 0 0 auto !important;
    width: auto !important;
}
[class*="st-key-row_"] button p {
    white-space: nowrap;
}
/* 제목: 사용자 입력은 html.escape 후 그대로 표시 (마크다운 해석 없음) */
.todo-title {
    overflow-wrap: anywhere;
    line-height: 1.5;
}
.todo-title.done {
    text-decoration: line-through;
    opacity: 0.55;
}
/* 아이콘 버튼(수정/삭제): 아이콘은 CSS로 그리고(스크린리더가 읽지 않음),
   버튼 글자("수정"/"삭제")는 화면에서만 숨겨 스크린리더가 이 이름만 읽게 한다 */
[class*="st-key-edit_"] button p,
[class*="st-key-del_"]:not([class*="st-key-del_yes_"]):not([class*="st-key-del_no_"]) button p {
    position: absolute !important;
    width: 1px; height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
}
[class*="st-key-edit_"] button::before,
[class*="st-key-del_"]:not([class*="st-key-del_yes_"]):not([class*="st-key-del_no_"]) button::before {
    font-family: "Material Symbols Rounded";
    font-size: 1.25rem;
    line-height: 1;
    font-weight: normal;
    font-feature-settings: "liga";
}
[class*="st-key-edit_"] button::before { content: "edit" / ""; }
[class*="st-key-del_"]:not([class*="st-key-del_yes_"]):not([class*="st-key-del_no_"]) button::before {
    content: "delete" / "";
}
/* 진행률 카드: 퍼센트 줄과 카테고리별 숫자 3개도 모바일에서 한 줄 유지 */
.st-key-progress_card [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
}
.st-key-progress_card [data-testid="stColumn"] {
    min-width: 0 !important;
}
.st-key-progress_card h2 {
    white-space: nowrap;
}
</style>
"""

# Todo 항목 구조 (PRD 4.1)
# {
#     "id": str (UUID4),
#     "title": str (1~100자),
#     "category": "업무" | "개인" | "기타",
#     "done": bool,
#     "created_at": str (ISO 8601),
#     "updated_at": str (ISO 8601),
# }


# ── 저장 / 불러오기 ─────────────────────────────────────
@st.cache_resource
def file_lock() -> threading.Lock:
    """모든 세션(탭)이 공유하는 파일 잠금. 읽기-수정-쓰기가 서로 끼어들지 않게 한다."""
    return threading.Lock()


def save_todos(todos: list[dict]) -> None:
    """할 일 목록을 JSON 파일에 원자적으로 저장한다.

    같은 폴더의 임시 파일에 먼저 쓴 뒤 os.replace로 교체하므로,
    저장 도중 중단돼도 기존 파일이 손상되지 않는다.
    """
    payload = {"version": DATA_VERSION, "todos": todos}
    fd, tmp_path = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".todos-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, DATA_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def backup_data_file() -> Path:
    """현재 데이터 파일을 시각이 붙은 이름으로 백업한다. 기존 백업은 덮어쓰지 않는다."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DATA_FILE.with_name(f"{DATA_FILE.name}.{stamp}.bak")
    n = 1
    while dest.exists():
        dest = DATA_FILE.with_name(f"{DATA_FILE.name}.{stamp}-{n}.bak")
        n += 1
    shutil.copy2(DATA_FILE, dest)
    return dest


def _is_valid_todo(item) -> bool:
    """파일에서 읽은 항목이 Todo 구조를 갖췄는지 확인한다."""
    return (
        isinstance(item, dict)
        and REQUIRED_FIELDS <= item.keys()
        and isinstance(item["id"], str) and item["id"] != ""
        and isinstance(item["title"], str)
        and item["category"] in CATEGORIES
        and isinstance(item["done"], bool)
    )


def load_todos() -> tuple[list[dict], str | None]:
    """JSON 파일에서 할 일 목록을 불러온다.

    반환값: (할 일 목록, 경고 메시지 또는 None)
    - 파일이 없으면 빈 목록으로 새 파일을 만든다.
    - 파일이 손상됐으면 백업한 뒤 빈 목록으로 새로 저장한다.
    - 구조가 잘못됐거나 id가 겹치는 항목이 있으면 백업한 뒤 그 항목만 빼고 다시 저장한다.
      (다시 저장해 두므로 같은 경고와 백업이 반복되지 않는다)
    - 파일을 읽을 수 없으면(권한, 잠김 등) OSError를 그대로 올린다.
      이때 빈 목록으로 덮어쓰면 데이터가 사라지므로 호출한 쪽에서 처리한다.
    """
    if not DATA_FILE.exists():
        save_todos([])
        return [], None

    try:
        with DATA_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        todos = data["todos"]
        if not isinstance(todos, list):
            raise ValueError("todos 필드가 목록이 아닙니다.")
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as e:
        backup = backup_data_file()
        save_todos([])
        return [], (
            f"저장 파일이 손상되어 빈 목록으로 시작합니다. "
            f"기존 파일은 '{backup.name}'으로 백업했습니다. ({type(e).__name__})"
        )

    seen: set[str] = set()
    valid = []
    for t in todos:
        if _is_valid_todo(t) and t["id"] not in seen:
            seen.add(t["id"])
            valid.append(t)
    if len(valid) != len(todos):
        backup = backup_data_file()
        save_todos(valid)
        return valid, (
            f"형식이 잘못됐거나 중복된 항목 {len(todos) - len(valid)}개를 건너뛰었습니다. "
            f"원본은 '{backup.name}'으로 백업했습니다."
        )
    return valid, None


def file_signature() -> tuple[int, int] | None:
    """파일이 바뀌었는지 알아보기 위한 (수정 시각, 크기). 파일이 없으면 None."""
    try:
        s = DATA_FILE.stat()
        return s.st_mtime_ns, s.st_size
    except FileNotFoundError:
        return None


# ── 상태 / 메시지 ───────────────────────────────────────
def adopt(todos: list[dict]) -> None:
    """파일에서 읽은 목록을 이 세션의 목록으로 삼는다."""
    st.session_state.todos = todos
    st.session_state.file_sig = file_signature()


def sync_from_disk() -> None:
    """처음 실행했거나 다른 탭/사용자가 파일을 바꿨으면 파일에서 다시 불러온다."""
    if "todos" in st.session_state and st.session_state.file_sig == file_signature():
        return
    try:
        with file_lock():
            todos, warning = load_todos()
            adopt(todos)
    except OSError as e:
        st.session_state.setdefault("todos", [])
        st.session_state.setdefault("file_sig", None)
        set_msg("list", "error", f"저장 파일을 읽지 못했어요. 잠시 후 새로고침해 주세요. ({e})")
        return
    if warning:
        st.session_state.load_warning = warning


def init_state() -> None:
    """session_state 기본값을 채우고 파일과 동기화한다."""
    st.session_state.setdefault("load_warning", None)
    st.session_state.setdefault("editing_id", None)        # 편집 중인 항목 (한 번에 하나)
    st.session_state.setdefault("confirm_delete_id", None)  # 삭제 확인 중인 항목
    st.session_state.setdefault("confirm_clear_done", False)
    st.session_state.setdefault("msg", None)                # {"where", "level", "text"}
    st.session_state.setdefault("celebrate", False)         # 100% 달성 시 풍선 1회
    st.session_state.setdefault("show_limit", PAGE_SIZE)    # 목록에 보여줄 개수
    st.session_state.setdefault("last_add_at", None)        # 마지막 추가 시각 (더블클릭 방지)
    st.session_state.setdefault("filter_category", "전체")
    st.session_state.setdefault("filter_status", "전체")
    sync_from_disk()


def set_msg(where: str, level: str, text: str) -> None:
    """다음 화면에 한 번 표시할 메시지를 등록한다. where: 'add' | 'list' | todo id."""
    st.session_state.msg = {"where": where, "level": level, "text": text}


def show_msg(where: str) -> None:
    """where 위치에 등록된 메시지가 있으면 표시하고 지운다."""
    msg = st.session_state.get("msg")
    if msg and msg["where"] == where:
        getattr(st, msg["level"])(msg["text"])
        st.session_state.msg = None


# ── 할 일 로직 (PRD 8.2) ────────────────────────────────
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate_title(title: str) -> tuple[str, str | None]:
    """제목을 정리(strip)하고 검사한다. 반환값: (정리된 제목, 오류 메시지 또는 None)."""
    title = (title or "").strip()
    if not title:
        return title, "할 일 내용을 입력해 주세요."
    if len(title) > TITLE_MAX_LEN:
        return title, f"할 일은 {TITLE_MAX_LEN}자 이하로 입력해 주세요. (현재 {len(title)}자)"
    return title, None


def find_todo(todos: list[dict], todo_id: str) -> dict | None:
    """id로 할 일을 찾는다."""
    return next((t for t in todos if t["id"] == todo_id), None)


def calc_progress(todos: list[dict]) -> tuple[int, int, int]:
    """진행률을 계산한다. 반환값: (완료 수, 전체 수, 퍼센트). 0개면 0%."""
    total = len(todos)
    done = sum(1 for t in todos if t["done"])
    pct = round(done / total * 100) if total else 0
    return done, total, pct


def mutate(change: Callable[[list[dict]], str | None]) -> str | None:
    """최신 파일을 읽어 change(todos)로 고친 뒤 저장한다. 실패하면 오류 메시지를 반환한다.

    change는 목록을 직접 고치고, 바꿀 수 없으면 오류 메시지를 반환한다.
    잠금 안에서 읽기-수정-쓰기를 하므로 다른 탭의 변경을 덮어쓰지 않는다.
    이 변경으로 진행률이 100%가 되면 축하 효과를 예약한다.
    """
    with file_lock():
        try:
            todos, warning = load_todos()
        except OSError as e:
            return f"저장 파일을 읽지 못했어요. 잠시 후 다시 시도해 주세요. ({e})"
        if warning:
            st.session_state.load_warning = warning
        before = calc_progress(todos)[2]

        error = change(todos)
        if error is None:
            try:
                save_todos(todos)
            except OSError as e:
                error = f"저장에 실패했어요. 잠시 후 다시 시도해 주세요. ({e})"
                try:
                    todos, _ = load_todos()  # 화면을 실제 파일 내용으로 되돌린다
                except OSError:
                    return error
        adopt(todos)

    if error is None:
        _, total, after = calc_progress(todos)
        if total and after == 100 and before < 100:
            st.session_state.celebrate = True
    return error


def new_todo(title: str, category: str) -> dict:
    """새 할 일 항목을 만든다."""
    now = _now()
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "category": category,
        "done": False,
        "created_at": now,
        "updated_at": now,
    }


def visible_todos(category: str, status: str) -> list[dict]:
    """필터를 적용하고 정렬한 목록을 반환한다. 미완료 먼저, 그 안에서는 최신순."""
    todos = st.session_state.todos
    if category != "전체":
        todos = [t for t in todos if t["category"] == category]
    if status == "미완료":
        todos = [t for t in todos if not t["done"]]
    elif status == "완료":
        todos = [t for t in todos if t["done"]]
    # 목록은 추가 순서(=생성 순서)로 쌓이므로 역순이 최신순이다. sorted는 안정 정렬.
    return sorted(reversed(todos), key=lambda t: t["done"])


# ── 위젯 콜백 ───────────────────────────────────────────
def on_add() -> None:
    """추가 폼 제출."""
    title, error = validate_title(st.session_state.new_title)
    category = st.session_state.new_category
    last_add_at = st.session_state.last_add_at

    if error:
        # 더블클릭의 두 번째 제출(입력창이 이미 비워짐)은 경고 없이 무시한다.
        if not title and last_add_at and time.time() - last_add_at < DOUBLE_SUBMIT_SEC:
            return
        set_msg("add", "warning", error)
        return
    st.session_state.last_add_at = time.time()

    def change(todos: list[dict]) -> None:
        # 더블클릭하면 두 요청이 거의 동시에 와서 session_state로는 서로를 못 볼 수 있다.
        # 그래서 잠금 안에서 읽은 최신 파일을 기준으로, 같은 내용이 방금 추가됐으면 건너뛴다.
        now = datetime.now()
        for t in todos:
            if t["title"] == title and t["category"] == category:
                try:
                    age = (now - datetime.fromisoformat(t["created_at"])).total_seconds()
                except ValueError:
                    continue
                if 0 <= age < DOUBLE_SUBMIT_SEC + 1:  # created_at이 초 단위라 1초 여유
                    return
        todos.append(new_todo(title, category))

    error = mutate(change)
    if error:
        set_msg("add", "error", error)
        return

    st.session_state.new_title = ""  # 성공했을 때만 비운다. 카테고리 선택은 유지.

    # 새 항목이 현재 필터에 가려지면 필터를 풀어서 바로 보이게 한다.
    hidden = False
    if st.session_state.get("filter_category", "전체") not in ("전체", category):
        st.session_state.filter_category = "전체"
        hidden = True
    if st.session_state.get("filter_status", "전체") == "완료":
        st.session_state.filter_status = "전체"
        hidden = True
    st.toast("할 일을 추가했어요." + (" 필터를 '전체'로 바꿨어요." if hidden else ""), icon="✅")


def on_toggle(todo_id: str) -> None:
    """완료 체크박스 변경."""
    done = st.session_state[f"done_{todo_id}"]
    st.session_state.confirm_clear_done = False  # 남아 있던 일괄 삭제 확인창은 닫는다

    def change(todos: list[dict]) -> str | None:
        todo = find_todo(todos, todo_id)
        if todo is None:
            return MISSING_MSG
        todo["done"] = done
        todo["updated_at"] = _now()
        return None

    error = mutate(change)
    if error:
        set_msg("list", "warning", error)


def on_start_edit(todo_id: str) -> None:
    """편집 모드 진입. 이전 편집 입력값이 남지 않도록 위젯 상태를 비운다."""
    if st.session_state.editing_id not in (None, todo_id):
        return  # 다른 항목을 편집 중이면 무시 (버튼도 비활성화돼 있다)
    for key in (f"edit_title_{todo_id}", f"edit_cat_{todo_id}"):
        st.session_state.pop(key, None)
    st.session_state.editing_id = todo_id
    st.session_state.confirm_delete_id = None


def on_save_edit(todo_id: str) -> None:
    """편집 저장. 검증 실패 시 편집 모드를 유지한다."""
    title, error = validate_title(st.session_state[f"edit_title_{todo_id}"])
    if error:
        set_msg(todo_id, "warning", error)
        return
    category = st.session_state[f"edit_cat_{todo_id}"]

    def change(todos: list[dict]) -> str | None:
        todo = find_todo(todos, todo_id)
        if todo is None:
            return MISSING_MSG
        todo.update(title=title, category=category, updated_at=_now())
        return None

    error = mutate(change)
    if error:
        if find_todo(st.session_state.todos, todo_id) is None:
            st.session_state.editing_id = None
            set_msg("list", "warning", error)
        else:
            set_msg(todo_id, "error", error)
        return
    st.session_state.editing_id = None
    st.toast("할 일을 수정했어요.", icon="✏️")


def on_cancel_edit() -> None:
    """편집 취소. 원래 값은 그대로 유지된다."""
    st.session_state.editing_id = None


def on_ask_delete(todo_id: str) -> None:
    """삭제 버튼 첫 클릭: 확인 단계로."""
    st.session_state.confirm_delete_id = todo_id


def on_confirm_delete(todo_id: str) -> None:
    """삭제 확인. 이미 다른 탭에서 지워졌어도 결과는 같으므로 성공으로 본다."""
    def change(todos: list[dict]) -> None:
        todos[:] = [t for t in todos if t["id"] != todo_id]

    st.session_state.confirm_delete_id = None
    error = mutate(change)
    if error:
        set_msg("list", "error", error)
        return
    if st.session_state.editing_id == todo_id:
        st.session_state.editing_id = None
    st.toast("할 일을 삭제했어요.", icon="🗑️")


def on_cancel_delete() -> None:
    """삭제 취소."""
    st.session_state.confirm_delete_id = None


def on_ask_clear_done() -> None:
    """완료 항목 일괄 삭제 첫 클릭: 확인 단계로."""
    st.session_state.confirm_clear_done = True


def on_confirm_clear_done() -> None:
    """완료 항목 일괄 삭제 확인."""
    removed = []

    def change(todos: list[dict]) -> None:
        removed.extend(t["id"] for t in todos if t["done"])
        todos[:] = [t for t in todos if not t["done"]]

    st.session_state.confirm_clear_done = False
    error = mutate(change)
    if error:
        set_msg("list", "error", error)
        return
    if st.session_state.editing_id in removed:
        st.session_state.editing_id = None
    if st.session_state.confirm_delete_id in removed:
        st.session_state.confirm_delete_id = None
    st.toast(f"완료한 할 일 {len(removed)}개를 삭제했어요.", icon="🧹")


def on_cancel_clear_done() -> None:
    """완료 항목 일괄 삭제 취소."""
    st.session_state.confirm_clear_done = False


def on_show_more() -> None:
    """목록을 PAGE_SIZE개 더 보여준다."""
    st.session_state.show_limit += PAGE_SIZE


def on_dismiss_warning() -> None:
    """파일 경고를 닫는다."""
    st.session_state.load_warning = None


# ── 화면 구성 ───────────────────────────────────────────
def category_label(category: str) -> str:
    """선택 상자에 표시할 카테고리 이름 (아이콘 포함)."""
    return f"{CATEGORY_ICONS[category]} {category}"


def render_progress() -> None:
    """전체 진행률과 카테고리별 진행률 (PRD 3.5)."""
    done, total, pct = calc_progress(st.session_state.todos)

    with st.container(border=True, key="progress_card"):
        c_pct, c_text = st.columns([1.4, 3], vertical_alignment="center")
        c_pct.markdown(f"## {pct}%")
        c_text.markdown(f"**진행률** · {total}개 중 **{done}개** 완료")
        st.progress(pct / 100)

        cols = st.columns(len(CATEGORIES))
        for col, cat in zip(cols, CATEGORIES):
            c_done, c_total, c_pct = calc_progress(
                [t for t in st.session_state.todos if t["category"] == cat]
            )
            col.metric(category_label(cat), f"{c_done}/{c_total}", f"{c_pct}%",
                       delta_color="off", delta_arrow="off")

    if total and pct == 100:
        st.success("🎉 모든 할 일을 끝냈어요! 수고했어요.")
    if st.session_state.celebrate:
        st.session_state.celebrate = False
        st.balloons()


def render_add_form() -> None:
    """새 할 일 입력 폼 (PRD 3.1). [제목 | 카테고리 | 추가] 한 줄.

    max_chars를 쓰지 않는다. 쓰면 100자가 넘는 글을 붙여넣을 때 브라우저가 아무 안내 없이
    입력을 통째로 버리기 때문이다. 길이는 validate_title에서 검사해 경고로 알려준다.
    """
    with st.form("add_form", border=False):
        c_title, c_cat, c_btn = st.columns([6, 2, 1.4], vertical_alignment="bottom")
        c_title.text_input(
            "할 일", key="new_title",
            placeholder=f"할 일을 입력하고 Enter (최대 {TITLE_MAX_LEN}자)",
            label_visibility="collapsed",
        )
        c_cat.selectbox("카테고리", CATEGORIES, key="new_category",
                        format_func=category_label, label_visibility="collapsed")
        c_btn.form_submit_button("추가", icon=":material/add:", type="primary",
                                 on_click=on_add, width="stretch")
    show_msg("add")


def render_filters() -> tuple[str, str]:
    """카테고리 / 상태 필터 (PRD 3.2). 가로로 나란히."""
    c1, c2 = st.columns([3, 2])
    # 기본값은 init_state에서 session_state로 준다. (on_add가 필터를 바꾸므로 default= 와 함께 쓰면 경고)
    category = c1.segmented_control("카테고리", CATEGORY_FILTERS, key="filter_category",
                                    required=True, label_visibility="collapsed")
    status = c2.segmented_control("상태", STATUS_FILTERS, key="filter_status",
                                  required=True, label_visibility="collapsed")
    return category or "전체", status or "전체"


def render_edit_form(todo: dict) -> None:
    """편집 모드 한 줄."""
    tid = todo["id"]
    with st.form(f"edit_form_{tid}", border=False):
        st.text_input("할 일 수정", value=todo["title"], key=f"edit_title_{tid}",
                      label_visibility="collapsed")
        c_cat, c_save, c_cancel = st.columns([3, 1.2, 1.2], vertical_alignment="bottom")
        c_cat.selectbox("카테고리", CATEGORIES, index=CATEGORIES.index(todo["category"]),
                        key=f"edit_cat_{tid}", format_func=category_label,
                        label_visibility="collapsed")
        c_save.form_submit_button("저장", icon=":material/check:", type="primary",
                                  on_click=on_save_edit, args=(tid,), width="stretch")
        c_cancel.form_submit_button("취소", on_click=on_cancel_edit, width="stretch")
    show_msg(tid)


def render_todo(todo: dict) -> None:
    """할 일 한 줄: [체크 | 배지 | 제목 | 수정 | 삭제]. 위젯 key는 모두 todo id 기반이다."""
    tid = todo["id"]
    with st.container(border=True, key=f"row_{tid}"):
        if st.session_state.editing_id == tid:
            render_edit_form(todo)
            return

        # 체크 상태는 매번 파일 기준 값으로 맞춘다 (다른 탭에서 바뀐 경우 반영).
        st.session_state[f"done_{tid}"] = todo["done"]
        editing_other = st.session_state.editing_id is not None

        c_check, c_badge, c_title, c_edit, c_del = st.columns(
            [0.5, 1.2, 7, 1, 1], vertical_alignment="center")
        c_check.checkbox(f"완료 표시: {todo['title']}", key=f"done_{tid}",
                         on_change=on_toggle, args=(tid,), label_visibility="collapsed")
        with c_badge.container(key=f"badge_{tid}"):
            st.badge(todo["category"], icon=CATEGORY_ICONS[todo["category"]],
                     color=CATEGORY_COLORS[todo["category"]])
        done_cls = " done" if todo["done"] else ""
        c_title.html(f'<div class="todo-title{done_cls}">{html.escape(todo["title"])}</div>')
        c_edit.button("수정", key=f"edit_{tid}", type="tertiary", disabled=editing_other,
                      help="다른 항목을 편집 중이에요" if editing_other else "수정",
                      on_click=on_start_edit, args=(tid,))
        c_del.button("삭제", key=f"del_{tid}", help="삭제", type="tertiary",
                     on_click=on_ask_delete, args=(tid,))

        if st.session_state.confirm_delete_id == tid:
            c_q, c_yes, c_no = st.columns([4, 1.2, 1.2], vertical_alignment="center")
            c_q.markdown(":red[**이 할 일을 삭제할까요?**]")
            c_yes.button("삭제", key=f"del_yes_{tid}", type="primary",
                         on_click=on_confirm_delete, args=(tid,), width="stretch")
            c_no.button("취소", key=f"del_no_{tid}", on_click=on_cancel_delete,
                        width="stretch")
        show_msg(tid)


def render_list(category: str, status: str) -> None:
    """할 일 목록 (PRD 3.2~3.4). 많을 때는 PAGE_SIZE개씩 나눠 보여준다."""
    show_msg("list")

    if not st.session_state.todos:
        st.info("할 일이 없어요. 위에서 새 할 일을 추가해 보세요!", icon="📝")
        return

    todos = visible_todos(category, status)
    if not todos:
        st.info("조건에 맞는 할 일이 없어요. 필터를 바꿔 보세요.", icon="🔍")
    limit = st.session_state.show_limit
    for todo in todos[:limit]:
        render_todo(todo)
    if len(todos) > limit:
        st.button(f"더 보기 ({len(todos) - limit}개 남음)", icon=":material/expand_more:",
                  type="tertiary", on_click=on_show_more)

    done_count = sum(1 for t in st.session_state.todos if t["done"])
    if not done_count:
        st.session_state.confirm_clear_done = False
        return
    if st.session_state.confirm_clear_done:
        c_q, c_yes, c_no = st.columns([4, 1.2, 1.2], vertical_alignment="center")
        c_q.markdown(f":red[**완료한 할 일 {done_count}개를 모두 삭제할까요?**]")
        c_yes.button("삭제", key="clear_yes", type="primary",
                     on_click=on_confirm_clear_done, width="stretch")
        c_no.button("취소", key="clear_no", on_click=on_cancel_clear_done, width="stretch")
    else:
        st.button(f"완료한 할 일 비우기 ({done_count}개)", icon=":material/cleaning_services:",
                  type="tertiary", on_click=on_ask_clear_done)


def render_load_warning() -> None:
    """파일 경고. 사용자가 닫을 때까지 보여준다."""
    if not st.session_state.load_warning:
        return
    c_w, c_x = st.columns([8, 1.2], vertical_alignment="center")
    c_w.warning(st.session_state.load_warning)
    c_x.button("닫기", key="dismiss_warning", on_click=on_dismiss_warning, width="stretch")


def main() -> None:
    """앱 진입점. 화면 순서는 PRD 5장 와이어프레임을 따른다."""
    st.set_page_config(page_title="To Do List", page_icon="✅", layout="centered")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    init_state()

    st.title("✅ To Do List")
    render_load_warning()
    render_progress()
    render_add_form()
    category, status = render_filters()
    render_list(category, status)


main()
