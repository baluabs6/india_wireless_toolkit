from setuptools import setup, find_packages

setup(
    name="india-wireless-toolkit",
    version="2.0.0",
    description="Scripts covering current issues in India's wireless/telecom sector",
    packages=find_packages(),
    install_requires=[
        "matplotlib>=3.7",
        "requests>=2.31",
        "beautifulsoup4>=4.12",
        "redis>=5.0",
        "pyyaml>=6.0",
        "plotly>=5.20",
        "feedparser>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "india-wireless-toolkit=india_wireless_toolkit.cli:main",
        ],
    },
    python_requires=">=3.8",
    include_package_data=True,
)
