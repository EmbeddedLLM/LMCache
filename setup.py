from setuptools import find_packages, setup
from torch.utils import cpp_extension
import torch
import os


def _is_hip():
    if torch.cuda.is_available() and torch.version.hip:
        return True
    else:
        return False


installed_dependencies = [
    "numpy==1.26.4",
    "aiofiles",
    "pyyaml",
    "redis",
    "safetensors",
    "transformers",
    "psutil",
    "aiohttp",
    "torchac_cuda >= 0.2.5",
    "sortedcontainers"
]

if not _is_hip():
    installed_dependencies.append([
        "torch >= 2.2.0",
        "nvtx",
    ])

extra_compile_args = {'cxx': ['-O3']}
define_macros = []

if _is_hip():
    rocm_home = os.environ.get('ROCM_HOME', '/opt/rocm')
    hip_include = os.path.join(rocm_home, 'include')
    hipcub_include = os.path.join(rocm_home, 'include/hipcub')

    extra_compile_args['hip'] = [
        '-O3', f'-I{hip_include}', f'-I{hipcub_include}'
    ]
    define_macros.append(('__HIP_PLATFORM_HCC__', '1'))
else:
    extra_compile_args['nvcc'] = ['-O3']

ext_modules = [
    cpp_extension.CUDAExtension('lmcache.c_ops', [
        'csrc/pybind.cpp',
        'csrc/mem_kernels.cu',
        'csrc/cal_cdf.cu',
        'csrc/ac_enc.cu',
        'csrc/ac_dec.cu',
    ],
                                extra_compile_args=extra_compile_args,
                                define_macros=define_macros),
]

cmdclass = {'build_ext': cpp_extension.BuildExtension}

setup(
    name="lmcache",
    version="0.1.4",
    description="LMCache: prefill your long contexts only once",
    author="LMCache team",
    author_email="lmcacheteam@gmail.com",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(exclude=("csrc")),
    install_requires=installed_dependencies,
    ext_modules=ext_modules,
    cmdclass=cmdclass,
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
