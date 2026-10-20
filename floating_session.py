"""悬浮窗的后台会话控制，不依赖任何 GUI 控件。"""

from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Thread
from typing import Callable, Literal

from deepseek_client import DeepSeekClient, ROLE_LABELS


@dataclass(frozen=True)
class ChatResult:
    request_id: int
    prompt: str
    reply: str | None = None
    error: str | None = None
    kind: Literal["initialization", "project_selection", "answer"] = "answer"
    projects: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Completion:
    result: ChatResult
    client: DeepSeekClient | None = None


class ChatSession:
    """在 UI 线程调用公开方法；后台线程只调用客户端并写入结果队列。"""

    def __init__(self, client_factory: Callable[[], DeepSeekClient] = DeepSeekClient) -> None:
        self.history: list[dict[str, str]] = []
        self._client_factory = client_factory
        self._results: SimpleQueue[_Completion] = SimpleQueue()
        self._client: DeepSeekClient | None = None
        self._role = "candidate"
        self._initialized = False
        self._projects: tuple[str, ...] = ()
        self._project_order: tuple[str, ...] = ()
        self._project_order_confirmed = False
        self._ready_message: str | None = None
        self._next_request_id = 0
        self._active_request_id: int | None = None
        self._active_kind: Literal["initialization", "answer"] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._active_request_id is not None

    @property
    def ready(self) -> bool:
        return self._initialized and self._client is not None and not self._closed

    @property
    def ready_message(self) -> str | None:
        return self._ready_message

    @property
    def role(self) -> str:
        return self._role

    @property
    def projects(self) -> tuple[str, ...]:
        return self._projects

    @property
    def project_order(self) -> tuple[str, ...]:
        return self._project_order

    @property
    def project_order_confirmed(self) -> bool:
        return self._project_order_confirmed and not self._closed

    @property
    def awaiting_project_order(self) -> bool:
        return (self._client is not None and not self._project_order_confirmed
                and not self.busy and not self._closed)

    def set_role(self, role: str) -> None:
        """空闲时切换回答身份，清空旧问答并保留资料、项目偏好及准备状态。"""
        self._check_open()
        if not isinstance(role, str) or role not in ROLE_LABELS:
            raise ValueError("请选择面试者或面试官角色。")
        if self.busy:
            raise ValueError("正在处理请求，请稍候再切换角色。")
        if role == self._role:
            return
        if self._client is not None:
            self._client.set_role(role)
        self._role = role
        self.history.clear()

    def initialize(self) -> int:
        """先识别项目供用户排序；确认后的重试保留同一客户端及偏好。"""
        self._check_open()
        if self.ready:
            raise ValueError("会话已经完成初始化。")
        if self.busy:
            raise ValueError("正在初始化，请稍候。")
        if self._client is not None and self._project_order_confirmed:
            return self._start("initialization", self._initialize, self._client)
        # 待选状态重新初始化表示资料已经更新，需要重新读取并识别。
        self._client = None
        self._projects = ()
        self._project_order = ()
        self._ready_message = None
        return self._start("initialization", self._detect_projects)

    def confirm_projects(self, order: list[str] | tuple[str, ...]) -> int:
        """显式确认完整项目顺序，然后后台请求 AI 的准备回复。"""
        self._check_open()
        if not self.awaiting_project_order:
            raise ValueError("请等待项目识别完成后再确认排序。")
        if (not isinstance(order, (list, tuple))
                or any(not isinstance(name, str) for name in order)
                or len(order) != len(self._projects)
                or len(set(order)) != len(order)
                or set(order) != set(self._projects)):
            raise ValueError("请为所有项目排序，每个项目必须且只能出现一次。")
        ordered_names = tuple(order)
        self._client.set_project_order(ordered_names)
        self._project_order = ordered_names
        self._project_order_confirmed = True
        return self._start("initialization", self._initialize, self._client)

    def reload_materials(self) -> int:
        """准备失败后重新读取资料并识别项目；旧排序必须再次确认。"""
        self._check_open()
        if self.ready:
            raise ValueError("会话已经完成初始化，请重新启动以更换面试资料。")
        if self.busy:
            raise ValueError("正在初始化，请稍候再重新加载资料。")
        self._client = None
        self._initialized = False
        self._projects = ()
        self._project_order = ()
        self._project_order_confirmed = False
        self._ready_message = None
        return self.initialize()

    def submit(self, prompt: str) -> int:
        """提交一轮问答，立即返回请求编号，不阻塞窗口。"""
        self._check_open()
        if not self.ready:
            raise ValueError("请先完成会话初始化。")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("请输入问题。")
        if self.busy:
            raise ValueError("正在生成回答，请稍候。")

        history = [dict(message) for message in self.history]
        return self._start("answer", self._request, self._client, prompt, history)

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("会话已经关闭。")

    def _start(self, kind: Literal["initialization", "answer"], target: Callable, *args) -> int:
        self._next_request_id += 1
        request_id = self._next_request_id
        self._active_request_id = request_id
        self._active_kind = kind
        worker = Thread(target=target, args=(request_id, *args), daemon=True)
        try:
            worker.start()
        except Exception:
            self._active_request_id = None
            self._active_kind = None
            raise
        return request_id

    def _detect_projects(self, request_id: int) -> None:
        try:
            client = self._client_factory()
            if self._role != "candidate":
                client.set_role(self._role)
            projects = tuple(client.detect_projects())
            if projects:
                reply = (f"已识别到 {len(projects)} 个项目。你最熟悉哪个项目？"
                         "请按熟悉程度从高到低排序，确认后再开始面试。")
            else:
                reply = "未从简历中识别到项目。请确认直接继续，或补充简历后重新识别。"
            result = ChatResult(request_id, "", reply=reply, kind="project_selection", projects=projects)
            completion = _Completion(result, client)
        except Exception as exc:
            result = ChatResult(request_id, "", error=str(exc) or type(exc).__name__, kind="initialization")
            completion = _Completion(result)
        self._results.put(completion)

    def _initialize(self, request_id: int, client: DeepSeekClient) -> None:
        try:
            reply = client.initialize()
            result = ChatResult(request_id, "", reply=reply, kind="initialization")
            completion = _Completion(result, client)
        except Exception as exc:
            result = ChatResult(request_id, "", error=str(exc) or type(exc).__name__, kind="initialization")
            completion = _Completion(result)
        self._results.put(completion)

    def _request(self, request_id: int, client: DeepSeekClient, prompt: str, history: list[dict[str, str]]) -> None:
        try:
            reply = client.chat(prompt, history=history)
            result = ChatResult(request_id, prompt, reply=reply)
        except Exception as exc:
            result = ChatResult(request_id, prompt, error=str(exc) or type(exc).__name__)
        self._results.put(_Completion(result))

    def poll(self) -> list[ChatResult]:
        """取回当前会话的结果；只有成功的完整问答会写入历史。"""
        accepted: list[ChatResult] = []
        while True:
            try:
                completion = self._results.get_nowait()
            except Empty:
                return accepted
            result = completion.result
            if self._closed or result.request_id != self._active_request_id:
                continue
            self._active_request_id = None
            self._active_kind = None
            if result.error is None and result.reply is not None:
                if result.kind == "project_selection":
                    self._client = completion.client
                    self._projects = result.projects
                elif result.kind == "initialization":
                    self._client = completion.client
                    self._initialized = True
                    self._ready_message = result.reply
                else:
                    self.history.extend([
                        {"role": "user", "content": result.prompt},
                        {"role": "assistant", "content": result.reply},
                    ])
            accepted.append(result)

    def clear(self) -> None:
        """清空问答并丢弃旧回答，保留已加载的资料和初始化状态。"""
        self.history.clear()
        if self._active_kind == "answer":
            self._active_request_id = None
            self._active_kind = None

    def close(self) -> None:
        """关闭会话；晚到的后台结果不会再显示或写入历史。"""
        self._closed = True
        self.history.clear()
        self._client = None
        self._initialized = False
        self._projects = ()
        self._project_order = ()
        self._project_order_confirmed = False
        self._ready_message = None
        self._active_request_id = None
        self._active_kind = None
