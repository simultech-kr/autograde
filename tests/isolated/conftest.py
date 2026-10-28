import pytest


def pytest_addoption(parser):
    parser.addoption("--isolated-grading-image", default=None,
        help="Preloaded immutable Linux OCI image with Python3, GCC and G++ for real container acceptance")
    parser.addoption("--isolated-grading-runtime", default="docker", choices=("docker", "podman"))


@pytest.fixture
def isolated_image(request):
    image = request.config.getoption("--isolated-grading-image")
    if not image:
        pytest.skip("Real OCI acceptance requires --isolated-grading-image; no host fallback")
    return image


@pytest.fixture
def isolated_runtime(request):
    return request.config.getoption("--isolated-grading-runtime")
