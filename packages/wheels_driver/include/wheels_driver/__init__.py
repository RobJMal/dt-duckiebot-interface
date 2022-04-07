"""
The wheels driver consists of a single class that handles the communication with the wheel drivers.

.. autoclass:: wheels_driver.DaguWheelsDriver

"""

from dt_robot_utils import get_robot_hardware, RobotHardware


if get_robot_hardware() != RobotHardware.VIRTUAL:
    # this breaks if imported when running on a virtual robot
    from .dagu_wheels_driver import DaguWheelsDriver

from .virtual_wheels_driver import VirtualWheelsDriver


