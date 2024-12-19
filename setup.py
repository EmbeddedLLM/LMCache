import torch
from setuptools import find_packages, setup


def _is_hip():
    if torch.cuda.is_available() and torch.version.hip:
        return True
    else:
        return False


installed_dependencies = [
    "numpy==1.26.4",
    "pyyaml",
    "redis",
    "safetensors",
    "transformers",
    "torchac_cuda >= 0.2.5",
]

if not _is_hip():
    installed_dependencies.append([
        "torch >= 2.2.0",
        "nvtx",
    ])

setup(
    name="lmcache",
    version="0.1.3",
    description="LMCache: prefill your long contexts only once",
    author="LMCache team",
    author_email="lmcacheteam@gmail.com",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(),
    install_requires=[
        "torch >= 2.2.0",
        "numpy==1.26.4",
        "pyyaml",
        "redis",
        "nvtx",
        "safetensors",
        "transformers",
        "torchac_cuda >= 0.2.5",
    ],
    classifiers=[
        # Trove classifiers
        # Full list at https://pypi.org/classifiers/
        "Development Status :: 3 - Alpha",
        "Environment :: GPU",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
    ],
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            # Add command-line scripts here
            # e.g., "my_command=my_package.module:function"
            "lmcache_server=lmcache.server.__main__:main",
        ],
    },
)
