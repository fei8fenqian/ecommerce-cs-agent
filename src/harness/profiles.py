"""V7-01 不可变 AgentProfile Registry。"""

from types import MappingProxyType
from typing import Mapping

from harness.types import (
    AgentProfile,
    ProfileHash,
    ProfileId,
    ProfileMismatchError,
    ProfileNotAvailableError,
    ProfileVersion,
    TaskKind,
    ToolId,
)


class AgentProfileRegistry:
    """仅提供服务端已发布 Profile 的不可变 Registry。

    API、模型、请求体和 Tool 输入都不能把 Profile 放入此 Registry 或选择任意 Profile。
    V7-01 唯一公开入口是按固定 TaskKind 取得首版 Profile。
    """

    def __init__(self) -> None:
        """注册首版唯一的只读客服知识 Profile。"""
        profile = AgentProfile.create(
            profile_id=ProfileId("support-knowledge-readonly"),
            version=ProfileVersion(1),
            task_kinds=frozenset({TaskKind.SUPPORT_KNOWLEDGE_ASSIST}),
            allowed_tools=frozenset(
                {
                    ToolId.KNOWLEDGE_SEARCH_V1,
                    ToolId.POLICY_EXCERPT_V1,
                    ToolId.TICKET_AUTHORIZED_SUMMARY_V1,
                }
            ),
        )
        self._profiles: Mapping[tuple[ProfileId, ProfileVersion], AgentProfile] = MappingProxyType(
            {(profile.profile_id, profile.version): profile}
        )

    def profile_for_task(self, task_kind: TaskKind) -> AgentProfile:
        """返回服务端为 V7-01 任务类别固定选择的已发布 Profile。

        Args:
            task_kind: 已经由 Task API 解析的受控任务类别。

        Returns:
            不可变、已发布且只能收缩权限的 AgentProfile。

        Raises:
            ProfileNotAvailableError: task_kind 在 V7-01 尚未启用时抛出。
            TypeError: task_kind 不是 TaskKind 枚举时抛出。
        """
        if not isinstance(task_kind, TaskKind):
            raise TypeError("task_kind must be TaskKind")
        for profile in self._profiles.values():
            if task_kind in profile.task_kinds:
                return profile
        raise ProfileNotAvailableError(f"no published V7-01 Profile for task kind {task_kind.value}")

    def verify_persisted(
        self,
        profile_id: ProfileId,
        version: ProfileVersion,
        profile_hash: ProfileHash,
        task_kind: TaskKind,
    ) -> AgentProfile:
        """复核持久化 Profile 引用没有被替换、回写或扩大权限。

        Args:
            profile_id: Task/Run 已保存的 Profile ID。
            version: Task/Run 已保存的 Profile 版本。
            profile_hash: Task/Run 已保存的内容 hash。
            task_kind: 需要恢复或继续的受控任务类别。

        Returns:
            与已持久化 ID、版本和 hash 完全一致的已发布 Profile。

        Raises:
            ProfileNotAvailableError: ID/版本不存在或该版本不允许任务类别时抛出。
            ProfileMismatchError: 保存的 hash 与当前不可变版本不一致时抛出。
        """
        profile = self._profiles.get((profile_id, version))
        if profile is None or task_kind not in profile.task_kinds:
            raise ProfileNotAvailableError("persisted Profile is unavailable for this task")
        if profile.profile_hash != profile_hash:
            raise ProfileMismatchError("persisted Profile hash does not match the published version")
        return profile
