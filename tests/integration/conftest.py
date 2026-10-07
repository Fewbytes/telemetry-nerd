import time

import httpx
import pytest
from testcontainers.core.container import DockerContainer

# CI pulls this from Docker Hub, which rate-limits anonymous pulls per IP (shared runner IPs can
# hit it). If that flakes the required gate, mirror the image to GHCR and point VM_IMAGE there.
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


# Same Docker Hub caveat as VM_IMAGE; Elastic's own registry for Elasticsearch.
ES_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.15.3"
OS_IMAGE = "opensearchproject/opensearch:2.17.1"


def _wait_green(url: str, container: DockerContainer, what: str, deadline_s: float = 180) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            r = httpx.get(f"{url}/_cluster/health?wait_for_status=yellow&timeout=1s", timeout=3)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            container.stop()
            raise RuntimeError(f"{what} did not become healthy")
        time.sleep(1)


@pytest.fixture(scope="session")
def es_url():
    container = (
        DockerContainer(ES_IMAGE)
        .with_exposed_ports(9200)
        .with_env("discovery.type", "single-node")
        .with_env("xpack.security.enabled", "false")
        .with_env("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"
    _wait_green(url, container, "Elasticsearch")
    yield url
    container.stop()


@pytest.fixture(scope="session")
def os_url():
    container = (
        DockerContainer(OS_IMAGE)
        .with_exposed_ports(9200)
        .with_env("discovery.type", "single-node")
        .with_env("DISABLE_SECURITY_PLUGIN", "true")
        .with_env("DISABLE_INSTALL_DEMO_CONFIG", "true")
        .with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
    )
    container.start()
    url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9200)}"
    _wait_green(url, container, "OpenSearch")
    yield url
    container.stop()
