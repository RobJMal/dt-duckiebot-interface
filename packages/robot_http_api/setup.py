# # ! DO NOT MANUALLY INVOKE THIS setup.py, USE CATKIN INSTEAD
from distutils.core import setup
from catkin_pkg.python_setup import generate_distutils_setup

setup_args = generate_distutils_setup(
    packages=["dt_robot_rest_api", "hardware_test_robot_host"],
    package_dir={"": "include"},
)
setup(**setup_args)
