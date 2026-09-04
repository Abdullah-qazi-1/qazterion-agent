import glob
from setuptools import setup, find_packages

py_modules = [f[:-3] for f in glob.glob("qz_*.py")] + ["generate_config"]

setup(
    name="qazterion",
    version="2.0.0",
    description="Autonomous Multi-Provider Coding Agent and Cross-Platform CLI",
    author="Abdullah",
    author_email="abdullahizaq321@gmail.com",
    packages=find_packages(include=["qz_*"]),
    py_modules=py_modules,
    install_requires=[
        "litellm[proxy]>=1.40.0",
        "openai>=1.20.0",
        "PyYAML>=6.0.0",
        "cryptography>=41.0.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0.0",
        "python-dotenv>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "qazterion = qz_cli.app:main",
            "qz = qz_cli.app:main",
        ],
    },
)
