"""
测试异步事件循环在线程池中的修复

问题：在线程池中调用 asyncio.get_event_loop() 会抛出 RuntimeError
解决：使用 asyncio.new_event_loop() 创建新的事件循环
"""

import asyncio
import pytest
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from tradingagents.dataflows.data_source_manager import DataSourceManager, ChinaDataSource


def test_asyncio_in_thread_pool():
    """测试在线程池中使用异步方法"""
    
    def run_in_thread():
        """在线程池中运行的函数"""
        # 这应该不会抛出 RuntimeError
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            # 在线程池中没有事件循环，创建新的
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # 测试运行一个简单的异步函数
        async def simple_async():
            await asyncio.sleep(0.01)
            return "success"
        
        result = loop.run_until_complete(simple_async())
        return result
    
    # 在线程池中执行
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(run_in_thread)
        result = future.result(timeout=5)
        assert result == "success"


def test_data_source_manager_in_thread_pool():
    """测试 DataSourceManager 在线程池中的使用"""
    
    def get_stock_data():
        """在线程池中获取股票数据"""
        manager = DataSourceManager()
        # 这应该不会抛出 RuntimeError
        # 注意：实际数据获取可能失败（如果没有配置API key），但不应该是事件循环错误
        try:
            result = manager.get_stock_data(
                symbol="000001",
                start_date="2025-01-01",
                end_date="2025-01-10",
                period="daily"
            )
            return result
        except Exception as e:
            # 如果是事件循环错误，测试失败
            if "There is no current event loop" in str(e):
                raise AssertionError(f"事件循环错误未修复: {e}")
            # 其他错误（如API配置问题）可以接受
            return f"其他错误（可接受）: {type(e).__name__}"
    
    # 在线程池中执行
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(get_stock_data)
        result = future.result(timeout=30)
        
        # 验证不是事件循环错误
        assert "There is no current event loop" not in str(result)
        print(f"✅ 测试通过，结果: {result[:200] if isinstance(result, str) else result}")


def test_multiple_threads():
    """测试多个线程同时使用异步方法"""
    
    def run_async_task(task_id):
        """在线程中运行异步任务"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        async def task():
            await asyncio.sleep(0.01)
            return f"Task {task_id} completed"
        
        return loop.run_until_complete(task())
    
    # 在多个线程中同时执行
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(run_async_task, i) for i in range(5)]
        results = [f.result(timeout=5) for f in futures]
        
        # 验证所有任务都成功完成
        assert len(results) == 5
        for i, result in enumerate(results):
            assert result == f"Task {i} completed"


def test_provider_call_inside_running_event_loop(monkeypatch):
    """同步数据源方法在已有事件循环中也应能调用异步 provider。"""

    class FakeProvider:
        async def get_historical_data(self, symbol, start_date, end_date, period):
            return pd.DataFrame(
                {
                    "date": ["2026-06-01", "2026-06-02"],
                    "open": [10.0, 10.5],
                    "high": [11.0, 11.5],
                    "low": [9.5, 10.0],
                    "close": [10.5, 11.0],
                    "volume": [1000, 1200],
                }
            )

        async def get_stock_basic_info(self, symbol):
            return {"name": "浪潮信息"}

    monkeypatch.setattr(
        "tradingagents.dataflows.providers.china.akshare.get_akshare_provider",
        lambda: FakeProvider(),
    )

    manager = object.__new__(DataSourceManager)
    manager._format_stock_data_response = lambda data, symbol, stock_name, start_date, end_date: (
        f"{stock_name}({symbol}) ok"
    )

    async def call_sync_method_from_async_context():
        return manager._get_akshare_data("000977", "2026-06-01", "2026-06-02")

    result = asyncio.run(call_sync_method_from_async_context())

    assert result == "浪潮信息(000977) ok"
    assert "event loop is already running" not in result


def test_get_stock_data_returns_string_when_fallback_succeeds(monkeypatch):
    """降级函数返回(result, source)时，对外接口仍必须返回字符串。"""

    manager = object.__new__(DataSourceManager)
    manager.current_source = ChinaDataSource.AKSHARE
    manager._get_akshare_data = lambda symbol, start_date, end_date, period: "❌ AKShare失败"
    manager._try_fallback_sources = lambda symbol, start_date, end_date, period="daily": (
        "fallback ok",
        "tushare",
    )

    result = manager.get_stock_data("000977", "2026-06-01", "2026-06-02")

    assert result == "fallback ok"
    assert isinstance(result, str)


if __name__ == "__main__":
    print("🧪 测试1: 线程池中的异步方法")
    test_asyncio_in_thread_pool()
    print("✅ 测试1通过\n")
    
    print("🧪 测试2: DataSourceManager 在线程池中")
    test_data_source_manager_in_thread_pool()
    print("✅ 测试2通过\n")
    
    print("🧪 测试3: 多线程并发")
    test_multiple_threads()
    print("✅ 测试3通过\n")
    
    print("🎉 所有测试通过！")
