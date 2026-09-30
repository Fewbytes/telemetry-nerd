import time

import httpx
import pytest
from testcontainers.core.container import DockerContainer

VM_IMAGE = "victoriametrics/victoria-metrics:v1.137.0"


@pytest.fixture(scope="session")
def vm_url():
    container = (
        DockerContainer(VM_IMAGE)
        .with_exposed_ports(8428)
        .with_command("-retentionPeriod=100y -search.latencyOffset=0s -search.disableCache")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8428)}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            container.stop()
            raise RuntimeError("VictoriaMetrics did not become healthy")
        time.sleep(0.3)
    yield url
    container.stop()
