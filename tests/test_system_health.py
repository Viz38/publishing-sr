import pytest
import asyncio
from unittest.mock import patch, MagicMock
from sr_common.utils import SystemHealthMonitor

class MockVirtualMemory:
    def __init__(self, percent):
        self.percent = percent

@pytest.fixture
def mock_psutil():
    with patch("psutil.cpu_percent") as mock_cpu, \
         patch("psutil.virtual_memory") as mock_mem:
        yield mock_cpu, mock_mem

def test_system_health_ignores_cpu(mock_psutil):
    mock_cpu, mock_mem = mock_psutil
    # Set CPU to 100% (should be ignored) and Memory to 50% (healthy)
    mock_cpu.return_value = 100.0
    mock_mem.return_value = MockVirtualMemory(50.0)
    
    monitor = SystemHealthMonitor(cpu_threshold=80.0, mem_threshold=90.0)
    
    is_healthy, reason = monitor.is_healthy()
    
    assert is_healthy is True, "Monitor should return True when CPU is 100% but Memory is safe"
    assert reason == "Healthy"

def test_system_health_fails_on_memory(mock_psutil):
    mock_cpu, mock_mem = mock_psutil
    # Set CPU to 10% (safe) but Memory to 95% (unsafe)
    mock_cpu.return_value = 10.0
    mock_mem.return_value = MockVirtualMemory(95.0)
    
    monitor = SystemHealthMonitor(cpu_threshold=80.0, mem_threshold=90.0)
    
    is_healthy, reason = monitor.is_healthy()
    
    assert is_healthy is False, "Monitor should return False when Memory is unsafe"
    assert "Memory too high (95.0%)" in reason

@pytest.mark.asyncio
async def test_wait_for_resources_timeout(mock_psutil):
    mock_cpu, mock_mem = mock_psutil
    # Simulate a permanent memory leak
    mock_mem.return_value = MockVirtualMemory(99.0)
    monitor = SystemHealthMonitor(mem_threshold=85.0)
    
    with pytest.raises(TimeoutError) as excinfo:
        await monitor.wait_for_resources(timeout=1.0)
        
    assert "Resource saturation timeout: Memory too high" in str(excinfo.value)
