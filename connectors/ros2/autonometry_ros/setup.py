"""ament_python build of the Autonometry TurtleBot 4 observer."""

from glob import glob

from setuptools import setup

PACKAGE = "autonometry_ros"

setup(
    name=PACKAGE,
    version="0.0.1",
    packages=[PACKAGE],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE}"]),
        (f"share/{PACKAGE}", ["package.xml"]),
        (f"share/{PACKAGE}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Autonometry maintainers",
    maintainer_email="maintainers@autonometry.invalid",
    description="Read-only Autonometry observer for the TurtleBot 4.",
    license="Apache-2.0",
    entry_points={"console_scripts": [f"observer = {PACKAGE}.observer:main"]},
)
