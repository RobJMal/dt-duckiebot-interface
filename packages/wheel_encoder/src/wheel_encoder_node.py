#!/usr/bin/env python3

import rospy
import uuid

from duckietown_msgs.msg import WheelEncoderStamped, WheelsCmdStamped
from wheel_encoder import WheelEncoderDriver
from duckietown.dtros import DTROS, TopicType, NodeType

FORWARD = 1
REVERSE = -1

class WheelEncoderNode(DTROS):
    """Node handling a single wheel encoder.

        This node is responsible for reading data off of a single wheel encoders.
        Robots with N wheels will need to spin N instances of this node.
        This node is compatible with any rotary encoder that signals ticks as rising edges
        on a digital GPIO pin.

        For now we use the `wheel_cmd_executed` to determine if we are moving forwards or backwards.
        As a result, if you manually push the robot, you will get potentially incorrect output
        (we default to always forward in this case).

        Subscribers:
           ~wheels_cmd_executed (:obj:`WheemsCmdStamped`): The actual commands executed
        Publishers:
           ~data (:obj:`WheelEncoderStamped`): Publishes the cumulative number of ticks
                                                generated by the encoder.

    """

    def __init__(self, node_name):
        # Initialize the DTROS parent class
        super(WheelEncoderNode, self).__init__(
            node_name=node_name,
            node_type=NodeType.DRIVER
        )
        # get parameters
        self._name = rospy.get_param('~name')
        self._gpio_pin = rospy.get_param('~gpio')
        self._resolution = rospy.get_param('~resolution')
        self._configuration = rospy.get_param('~configuration')
        self._tick_no = 1

        # setup the driver
        self._driver = WheelEncoderDriver(self._gpio_pin, self._encoder_tick_cb)

        # publisher for wheel encoder ticks
        self._tick_pub = rospy.Publisher(
            "~tick",
            WheelEncoderStamped,
            queue_size=1,
            dt_topic_type=TopicType.DRIVER
        )

        # subscriber for the wheel command executed
        self.sub_wheels = rospy.Subscriber(
            "~wheels_cmd_executed",
            WheelsCmdStamped,
            self._wheels_cmd_executed_cb,
            queue_size=1
        )

    def _wheels_cmd_executed_cb(self,msg):
        if self._configuration == "left":
            if msg.vel_left >= 0:
                self._driver.direction = FORWARD
            else:
                self._driver.direction = REVERSE
        elif self._configuration == "right":
            if msg.vel_right >= 0:
                self._driver.direction = FORWARD
            else:
                self._driver.direction = REVERSE

    def _encoder_tick_cb(self, tick_no):
        """
        Callback that receives new ticks from the encoder.

            Args:
                tick_no (int): cumulative total number of ticks
        """
        self._tick_pub.publish(WheelEncoderStamped(
            data=tick_no,
            resolution=self._resolution,
            type=WheelEncoderStamped.ENCODER_TYPE_INCREMENTAL
        ))


if __name__ == '__main__':
    # Initialize the node with rospy
    rand = str(uuid.uuid4())[:8]
    node = WheelEncoderNode('wheel_encoder_%s' % (rand,))
    # Keep it spinning to keep the node alive
    rospy.spin()
