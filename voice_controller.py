"""把稳定语音转写串行交给聊天会话；公开方法均在 UI 线程调用。"""

from collections import deque

from floating_session import ChatResult, ChatSession


class VoiceController:
    """partial 仅预览，完整问题排队，失败由用户明确重试或跳过。"""

    def __init__(self, session: ChatSession) -> None:
        self.session = session
        self._preview = ""
        self._segments: list[str] = []
        self._seen_segments: set[str] = set()
        self._pending: deque[str] = deque()
        self._failed_prompt: str | None = None
        self._active_request_id: int | None = None
        self._active_prompt: str | None = None
        self._paused = False

    @property
    def preview(self) -> str:
        return self._preview

    @property
    def pending_count(self) -> int:
        """尚未提交的完整问题数；失败问题单独由 failed_prompt 表示。"""
        return len(self._pending)

    @property
    def failed_prompt(self) -> str | None:
        return self._failed_prompt

    def partial(self, utterance_id: str, text: str) -> None:
        """替换当前临时识别结果，保留此前跨连接累积的稳定正文。"""
        if utterance_id not in self._seen_segments:
            self._preview = "".join(self._segments) + text.strip()

    def final(self, utterance_id: str, text: str, endpoint: bool = True) -> None:
        """按分段 ID 去重；轮转分段须 endpoint=False，停顿结束才入队。"""
        if utterance_id in self._seen_segments:
            return
        self._seen_segments.add(utterance_id)
        text = text.strip()
        if text:
            self._segments.append(text)
        self._preview = "".join(self._segments)
        if endpoint:
            if self._preview:
                self._pending.append(self._preview)
            self._segments.clear()
            self._preview = ""

    def dispatch(self) -> str | None:
        """会话空闲时提交队首；提交失败仍保留队首，交由 UI 显示错误。"""
        if (self._paused or self._failed_prompt is not None or not self._pending
                or self._active_request_id is not None
                or not self.session.ready or self.session.busy):
            return None
        prompt = self._pending[0]
        request_id = self.session.submit(prompt)
        self._pending.popleft()
        self._active_request_id = request_id
        self._active_prompt = prompt
        return prompt

    def accept(self, result: ChatResult) -> None:
        """接收已被 ChatSession.poll 接受的结果，不自行操作问答历史。"""
        if result.kind != "answer" or result.request_id != self._active_request_id:
            return
        if result.error is not None:
            self._failed_prompt = self._active_prompt
        self._active_request_id = None
        self._active_prompt = None

    def retry(self) -> None:
        if self._failed_prompt is not None:
            self._pending.appendleft(self._failed_prompt)
            self._failed_prompt = None

    def skip_failed(self) -> None:
        self._failed_prompt = None

    def pause(self) -> None:
        """停止自动提交，保留完整待答问题，并舍弃未结束的语音片段。"""
        self._paused = True
        self._segments.clear()
        self._preview = ""

    def resume(self) -> None:
        self._paused = False

    def clear(self) -> None:
        """开始新对话；语音事件的代际过滤由上层采集会话负责。"""
        self._preview = ""
        self._segments.clear()
        self._seen_segments.clear()
        self._pending.clear()
        self._failed_prompt = None
        self._active_request_id = None
        self._active_prompt = None
        self.session.clear()
