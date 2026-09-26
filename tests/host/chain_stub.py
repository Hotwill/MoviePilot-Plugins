"""Chain 运行上下文测试替身。

宿主 ``_PluginBase.__init__`` 会构造 PluginChain，而 ChainBase 要求启动组合根
已装配运行上下文。这里按官方插件仓的做法用 Mock 装配，只为让真实构造路径
可以执行，不触碰任何宿主服务。
"""

from __future__ import annotations

from unittest.mock import Mock


def chain_context():
    """构造不启动宿主服务的最小 Chain 组合根。

    宿主 ``_PluginBase.__init__`` 会构造 PluginChain，而 ChainBase 要求启动
    组合根已装配运行上下文。测试里按官方插件仓的做法用 Mock 装配，
    只为让真实构造路径可以执行，不触碰任何宿主服务。
    """
    from app.application.chain.context import ChainRuntimeContext
    from app.application.configuration import ChainRuntimeConfig

    message_queue = Mock()
    message_queue.bind.return_value = Mock()
    return ChainRuntimeContext(
        module_manager=Mock(),
        plugin_manager=Mock(),
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue=message_queue,
        module_dispatcher_factory=Mock(return_value=Mock()),
        site_repository=Mock(),
        subscription_repository=Mock(),
        subscription_mutation_scope=Mock(),
        sync_subscription_mutation_scope=Mock(),
        subscription_delete_scope=Mock(),
        sync_subscription_delete_scope=Mock(),
        subscription_completion_scope=Mock(),
        rule_group_mutation_scope=Mock(),
        site_reference_mutation_scope=Mock(),
        download_history_repository=Mock(),
        transfer_history_repository=Mock(),
        transfer_admission_repository=Mock(),
        transfer_execution_repository=Mock(),
        media_server_repository=Mock(),
        download_failure_repository=Mock(),
        user_repository=Mock(),
        configuration=ChainRuntimeConfig(media_extensions=(".mkv", ".strm")),
    )
