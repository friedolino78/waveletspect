from setuptools import setup, find_packages

setup(
    name="waveletspect",
    version="0.1.0",
    description="Echtzeit Wavelet-Spektrogramm via JACK + GTK3/Cairo",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="mk",
    license="MIT",
    python_requires=">=3.10",
    install_requires=[
        "numpy>=1.24",
        "scipy>=1.10",
        "PyWavelets>=1.5",
        "JACK-Client>=0.5",
        "PyGObject>=3.42",
        "pycairo>=1.25",
    ],
    entry_points={
        "console_scripts": [
            "waveletspect=waveletspect:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: X11 Applications :: GTK",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Topic :: Multimedia :: Sound/Audio :: Analysis",
        "Topic :: Scientific/Engineering :: Visualization",
    ],
)
