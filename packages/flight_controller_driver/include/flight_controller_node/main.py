import argparse
import asyncio
import sys
import time
import traceback
from collections import defaultdict
from typing import List, Optional, Union

from dt_node_utils import NodeType
from dt_node_utils.decorators import sidecar
from dt_node_utils.node import Node
from dt_robot_utils.constants import RobotHardware
from dt_robot_utils.robot import get_robot_hardware
from dtps.ergo_ui import DTPSContext
from dtps_http.object_queue import TransformError
from dtps_http.structures import RawData
from duckietown_messages.actuators.drone_control import DroneControl
from duckietown_messages.actuators.drone_mode import DroneModeMsg, DroneModeResponse, Mode
from duckietown_messages.actuators.drone_motor_command import DroneMotorCommand
from duckietown_messages.sensors.attitude import Attitude
from duckietown_messages.sensors.battery import BatteryState
from duckietown_messages.sensors.linear_accelerations import LinearAccelerations
from duckietown_messages.sensors.angular_velocities import AngularVelocities
from duckietown_messages.standard.dictionary import Dictionary
from duckietown_messages.standard.header import Header
from flight_controller_driver.flight_controller_abs import DroneMode, FCError

from tiny_tf.tf import Transform
from tiny_tf.transformations import euler_from_quaternion

from flight_controller_node.types import FlightControllerConfiguration

if get_robot_hardware() == RobotHardware.VIRTUAL:
    from flight_controller_driver.flight_controller_virtual import (
        FlightControllerVirtual,
    )
else:
    from flight_controller_driver.flight_controller_physical import (
        FlightControllerPhysical,
    )
    
from flight_controller_driver import AttitudePidGains, Mode2RC


def near_zero(n):
    """ Set a number to zero if it is below a threshold value """
    return 0 if abs(n) < 0.0001 else n


class FlightControllerNode(Node):
    """
    A class that implements the communication channels with the flight controller board via USB.
    It reads data from the IMU and the PWM signals going to the motors while relaying the command
    [R, P, Y, T] provided via ROS.

    """

    def __init__(self, config: str):
        node_name: str = "flight_controller_node"
        super(FlightControllerNode, self).__init__(name=node_name,
                                               kind=NodeType.DRIVER,
                                               description="Flight controller driver")

        self.configuration: FlightControllerConfiguration = FlightControllerConfiguration.from_name(self.package, node_name, config)

        # IMU reference frame id
        self._imu_frame_id: str = f"{self._robot_name}/imu"

        # internal state
        self._last_imu_msg = None
        self._last_published_mode: Optional[DroneMode] = None
        self._requested_mode: DroneMode = DroneMode.DISARMED
        self._current_mode: DroneMode = DroneMode.DISARMED

        # heartbeats
        self._heartbeat_joystick = time.time()
        self._heartbeat_pid = time.time()
        self._heartbeat_altitude = time.time()
        self._heartbeat_state_estimator = time.time()
        self._heartbeat_thr = 1.0 # Threshold for heartbeat checks [s]

        mode_2_rc = Mode2RC(
            disarm=self.configuration.rc_commands.disarm,
            arm=self.configuration.rc_commands.arm,
            idle=self.configuration.rc_commands.idle,
            flying=self.configuration.rc_commands.flying
        )

        try:
            # (try to) connect to the flight controller board
            if get_robot_hardware() == RobotHardware.VIRTUAL:
                self._board = FlightControllerVirtual(
                    mode_to_rc_commands=mode_2_rc,
                    serial_config=self.configuration.serial,
                    sitl_config=self.configuration.sitl
                )
            else:
                self._board = FlightControllerPhysical(
                    mode_to_rc_commands=mode_2_rc,
                    device_ids=self.configuration.device
                )

        except Exception as e:
            raise e

        self.current_mode_queue : Optional[DTPSContext] = None

        # TODO: if update PID param failed with FC, ros param and FC PIDs are inconsistent
        # low priority. the params will be of the true values on next container start-up

        # obtain default PID values
        
        # TODO: reimplement updating PID values

        #initial_rpy_pids: AttitudePidGains = self._get_pid_values_board()
        # self._param_roll_P = DTParam('~roll_P', default=initial_rpy_pids.roll_p,
        #                              param_type=ParamType.INT)
        # self._param_roll_P.register_update_callback(
        #     lambda: self._board.set_pids_rpy(roll_p=self._param_roll_P.value))
        # self._param_roll_I = DTParam('~roll_I', default=initial_rpy_pids.roll_i,
        #                              param_type=ParamType.INT)
        # self._param_roll_I.register_update_callback(
        #     lambda: self._board.set_pids_rpy(roll_i=self._param_roll_I.value))
        # self._param_roll_D = DTParam('~roll_D', default=initial_rpy_pids.roll_d,
        #                              param_type=ParamType.INT)
        # self._param_roll_D.register_update_callback(
        #     lambda: self._board.set_pids_rpy(roll_d=self._param_roll_D.value))

        # self._param_pitch_P = DTParam('~pitch_P', default=initial_rpy_pids.pitch_p,
        #                               param_type=ParamType.INT)
        # self._param_pitch_P.register_update_callback(
        #     lambda: self._board.set_pids_rpy(pitch_p=self._param_pitch_P.value))
        # self._param_pitch_I = DTParam('~pitch_I', default=initial_rpy_pids.pitch_i,
        #                               param_type=ParamType.INT)
        # self._param_pitch_I.register_update_callback(
        #     lambda: self._board.set_pids_rpy(pitch_i=self._param_pitch_I.value))
        # self._param_pitch_D = DTParam('~pitch_D', default=initial_rpy_pids.pitch_d,
        #                               param_type=ParamType.INT)
        # self._param_pitch_D.register_update_callback(
        #     lambda: self._board.set_pids_rpy(pitch_d=self._param_pitch_D.value))

        # self._param_yaw_P = DTParam('~yaw_P', default=initial_rpy_pids.yaw_p,
        #                             param_type=ParamType.INT)
        # self._param_yaw_P.register_update_callback(
        #     lambda: self._board.set_pids_rpy(yaw_p=self._param_yaw_P.value))
        # self._param_yaw_I = DTParam('~yaw_I', default=initial_rpy_pids.yaw_i,
        #                             param_type=ParamType.INT)
        # self._param_yaw_I.register_update_callback(
        #     lambda: self._board.set_pids_rpy(yaw_i=self._param_yaw_I.value))
        # self._param_yaw_D = DTParam('~yaw_D', default=initial_rpy_pids.yaw_d,
        #                             param_type=ParamType.INT)
        # self._param_yaw_D.register_update_callback(
        #     lambda: self._board.set_pids_rpy(yaw_d=self._param_yaw_D.value))

        # store the command to send to the flight controller, initialize as disarmed
        self._command = self._board.mode_to_rc_command(DroneMode.DISARMED)
        self._last_command = self._board.mode_to_rc_command(DroneMode.DISARMED)


    def _switch_to_mode(self, mode: DroneMode, quiet: bool = False):
        """ Update desired mode """
        # switch mode
        # TODO: this is wrong, we can't wait for a new mode to come in to update _current_mode,
        #  it should be done according to the data coming from the flight controller
        self._current_mode = self._requested_mode
        self._requested_mode = mode
        if not quiet:
            self._compute_flight_commands()

    async def set_mode_transform(self, rd : RawData,) -> Union[RawData, TransformError]:
        """ Update desired mode """
        mode = DroneMode(DroneModeMsg.from_rawdata(rd).mode.value)
        do_switch = True
        # the user can only request to DISARM, ARM, and FLY
        if mode not in [DroneMode.DISARMED, DroneMode.ARMED, DroneMode.FLYING]:
            do_switch = False
        # make sure we are not violating a DISARM check
        if mode in [DroneMode.ARMED, DroneMode.FLYING] and self._should_disarm():
            mode = DroneMode.DISARMED
        # switch mode
        if do_switch:
            self._switch_to_mode(mode)
        
        await self.current_mode_queue.publish(
            DroneModeMsg(mode=Mode(self._requested_mode.value)).to_rawdata()
            )
        # respond
        return DroneModeResponse(
                previous_mode=Mode(self._current_mode.value),
                current_mode=Mode(self._requested_mode.value),
            ).to_rawdata()

    async def _srv_zero_yaw_cb(self, _):
        """ Zero yaw """
        
        # Read the current yaw value
        last_imu_msg = self._last_imu_msg
        if last_imu_msg is None:
            return RawData.cbor_from_native_object(
                {"success":False, "message":"No IMU data received yet"}
                )
        
        self._update_yaw_offset(last_imu_msg.orientation)

        # respond
        return RawData.cbor_from_native_object(
            {"success": True, "message": f"Yaw zeroed to {self.yaw_offset}"}
            )

    async def _update_yaw_offset(self, orientation : Transform) -> Union[RawData, TransformError]:
        """ Update the yaw offset based on the given orientation """
        _, _, yaw = euler_from_quaternion(orientation.quaternion)
        self._board.yaw_offset += yaw 

        self.loginfo(f"Yaw offset set to {yaw}")
        return TransformError("Not implemented")
    

    async def _srv_calibrate_imu_cb(self, _):
        """ Calibrate IMU """
        try:
            self._board.calibrate_imu()
        except Exception as e:
            traceback.print_exc()
            return RawData.cbor_from_native_object(
            {"success": False, "message": str(e)}
            )
        # respond
        return RawData.cbor_from_native_object(
            {"success": True, "message": "IMU calibrated"}
            )

    async def _flight_commands_cb(self, rd):
        msg = DroneControl.from_rawdata(rd)
        """ Store and send the flight commands if the current mode is FLYING """
        if self._requested_mode is DroneMode.FLYING:
            aux1 = self._board.mode_to_rc_command(DroneMode.IDLE)[4]
            aux2 = self._board.mode_to_rc_command(DroneMode.IDLE)[5]

            # compile command to be sent to the flight controller board
            self._command = [int(msg.roll), int(msg.pitch), int(msg.yaw), int(msg.throttle), int(aux1), int(aux2)]

    def _read_battery_status(self):
        """ Read battery status """
        voltage = self._board.voltage
        return BatteryState(
                voltage = voltage,
                present = True if voltage > 6.0 else False,  # ~5V: power from Pi | 7V to 12.6V: power from battery
            )

    async def worker(self):
        await self.dtps_init(self.configuration)
        # Create queues OUT
        executed_commands_queue: DTPSContext = await (self.context / "out" / "commands" / "executed").queue_create()
        self.current_mode_queue: DTPSContext = await (self.context / "out" / "mode" / "current").queue_create()

        # Expose queues to the switchboard
        await (self.switchboard / "flight_controller" / "mode" / "current").expose(self.current_mode_queue)
        await (self.switchboard / "flight_controller" / "commands" / "executed").expose(executed_commands_queue)

        # Create queues IN
        heartbeat_altitude: DTPSContext = await (self.context / 'in' / 'heartbeat' / 'altitude').queue_create() 
        heartbeat_joystick: DTPSContext = await (self.context / 'in' / 'heartbeat' / 'joystick').queue_create() 
        heartbeat_pid: DTPSContext = await (self.context / 'in' / 'heartbeat' / 'pid').queue_create() 
        heartbeat_state_estimator: DTPSContext = await (self.context / 'in' / 'heartbeat' / 'state_estimator').queue_create()
        commands_queue: DTPSContext = await (self.context / "in" / "commands").queue_create()
        


        # Subscribe heartbeat callbacks
        await heartbeat_altitude.subscribe(self._heartbeat_altitude_cb)
        await heartbeat_joystick.subscribe(self._heartbeat_joystick_cb)
        await heartbeat_pid.subscribe(self._heartbeat_pid_cb)
        await heartbeat_state_estimator.subscribe(self._heartbeat_state_estimator_cb)
        
        # Subscribe to commands queue
        await commands_queue.subscribe(self._flight_commands_cb)

        # Create services queues and add transforms
        zero_yaw_queue = await (self.context / "in" / "imu" / "zero_yaw").queue_create(transform=self._srv_zero_yaw_cb)
        calibrate_imu_queue = await (self.context / "in" / "calibrate_imu").queue_create(transform=self._srv_calibrate_imu_cb)
        set_mode_queue = await (self.context / "in" / "set_mode").queue_create(
            transform= self.set_mode_transform
            )
 
        # Expose queues to the switchboard
        await (self.switchboard / "flight_controller" / "commands").expose(commands_queue)
        await (self.switchboard / "flight_controller" / "mode" / "set").expose(set_mode_queue)
        await (self.switchboard / "imu" / "zero_yaw").expose(zero_yaw_queue)
        await (self.switchboard / "imu" / "calibrate").expose(calibrate_imu_queue)

        # Expose heartbeat queues to the switchboard
        await (self.switchboard / "heartbeat" / "altitude").expose(heartbeat_altitude)
        await (self.switchboard / "heartbeat" / "joystick").expose(heartbeat_joystick)
        await (self.switchboard / "heartbeat" / "pid").expose(heartbeat_pid)
        await (self.switchboard / "heartbeat" / "state_estimator").expose(heartbeat_state_estimator)
        

        # Expose node to the switchboard
        await self.dtps_expose()

        
        dt: float = 1.0 / self.configuration.frequency.commands
        while not self.is_shutdown:
            try:
                # if the current mode is anything other than disarmed, preform as safety check
                if self._requested_mode is not DroneMode.DISARMED:
                    # break the loop if a safety check has failed
                    if self._should_disarm():
                        self.loginfo("Should disarm.")
                        self._switch_to_mode(DroneMode.DISARMED)
                        # sleep for the remainder of the loop time
                        await asyncio.sleep(dt)
                        continue

                # noinspection PyBroadException
                try:                    
                    # update and send the flight commands to the board
                    self._compute_flight_commands()
                    await self._send_flight_commands(executed_commands_queue)

                    # publish the current mode
                    if self._last_published_mode != self._requested_mode:
                        await self.current_mode_queue.publish(DroneModeMsg(mode=self._requested_mode.value).to_rawdata())
                        self._last_published_mode = self._requested_mode

                except FCError:
                    self.logwarn("Could not talk to the flight controller" + str(FCError))
                    continue

                # sleep for the remainder of the loop time
                await asyncio.sleep(dt)
                
            except Exception:
                traceback.print_exc()
        
        self.loginfo('Shutdown received, disarming...')
        self._board.disarm()
        time.sleep(0.5)

    @sidecar
    async def worker_battery(self):
        await self.switchboard_ready.wait()

        battery_queue = await (self.context / "out" / "battery").queue_create()
        await (self.switchboard / "sensor" / "battery").expose(battery_queue)

        dt = 1.0 / self.configuration.frequency.battery

        while not self.is_shutdown:
            try:
                # read battery status
                battery_msg = self._read_battery_status()
                await battery_queue.publish(battery_msg.to_rawdata())
            except Exception:
                traceback.print_exc()
            finally:
                await asyncio.sleep(dt)

    @sidecar
    async def worker_motor_pwm(self):
        await self.switchboard_ready.wait()

        motors_queue = await (self.context / "out" / "motors").queue_create()
        await (self.switchboard / "actuator" / "motors").expose(motors_queue)

        dt = 1.0 / self.configuration.frequency.motors        

        while not self.is_shutdown:
            try:
                # read PWM signals going to the motors
                motor_msg = self._read_motor_pwm_signals()
                await motors_queue.publish(motor_msg.to_rawdata())
            except Exception:
                traceback.print_exc()
            finally:
                await asyncio.sleep(dt)
    
    @sidecar
    async def worker_imu(self):
        await self.switchboard_ready.wait()

        accelerations_queue = await (self.context / "out" / "acceleration" / "linear").queue_create()
        velocities_queue = await (self.context / "out" / "velocity" / "angular").queue_create()
        orientation_queue = await (self.context / "out" / "attitude").queue_create()
        # TODO: is raw the appropriate name here? In the context of IMUs a raw value is 
        #       typically one that is specified in the IMU's own measurement units, which are
        #       then converted to SI measurement units (m/s, rad/s, etc.) through an IMU-specific
        #       conversion factor. Is this the case here?
        raw_queue = await (self.context / "out" / "raw").queue_create()

        await (self.switchboard / "sensor" / "imu" / "accelerometer").expose(accelerations_queue)
        await (self.switchboard / "sensor" / "imu" / "gyroscope"/ "velocity").expose(velocities_queue)
        await (self.switchboard / "sensor" / "imu" / "gyroscope"/ "orientation").expose(orientation_queue)
        await (self.switchboard / "sensor" / "imu" / "raw").expose(raw_queue)

        dt = 1.0 / self.configuration.frequency.imu

        while not self.is_shutdown:
            try:
                # process acceleration data
                a_x, a_y, a_z = self._board.acceleration
                acc : List[float] = [a_x, a_y, a_z]
                acceleration_message = LinearAccelerations(
                    x=near_zero(a_x),
                    y=near_zero(a_y),
                    z=near_zero(a_z),
                )
                omega_x, omega_y, omega_z = self._board.gyro
                
                angular_velocity_message = AngularVelocities(
                    x=omega_x,
                    y=omega_y,
                    z=omega_z
                )
                vel : List[float] = [omega_x, omega_y, omega_z]
                # process attitude data
                roll, pitch, yaw = self._board.attitude
                orientation : List[float] = [roll, pitch, yaw]
                orientation_msg = Attitude(
                    roll=near_zero(roll),
                    pitch=near_zero(pitch),
                    yaw=near_zero(yaw),
                )
            except Exception as e:
                traceback.print_exc()
                self.logwarn(f"IMU Comm Loss: {e}")
            else:
                # pack raw data
                raw: Dictionary = Dictionary(data={
                    "header": Header(frame=self._imu_frame_id),
                    "linear_accelerations": acc,
                    "angular_velocities": vel,
                    "orientation": orientation
                })
                await accelerations_queue.publish(acceleration_message.to_rawdata())
                await orientation_queue.publish(orientation_msg.to_rawdata())
                await velocities_queue.publish(angular_velocity_message.to_rawdata())
                await raw_queue.publish(raw.to_rawdata())
            finally:
                await asyncio.sleep(dt)


    def _compute_flight_commands(self):
        """ Set command values if the mode is ARMED or DISARMED """
        if self._requested_mode is DroneMode.DISARMED:
            # disarm
            self._command = self._board.mode_to_rc_command(DroneMode.DISARMED)
            self._switch_to_mode(DroneMode.DISARMED, quiet=True)

        elif self._requested_mode is DroneMode.ARMED:
            # arm
            if self._current_mode is DroneMode.DISARMED:
                # not yet armed
                self._command = self._board.mode_to_rc_command(DroneMode.ARMED)
                self._switch_to_mode(DroneMode.ARMED, quiet=True)

            elif self._current_mode is DroneMode.ARMED:
                # already armed
                self._command = self._board.mode_to_rc_command(DroneMode.ARMED)

 
    
    async def _send_flight_commands(self, queue : DTPSContext):
        """ Send commands to the flight controller board """
        try:
            self._board.send_command(self._command)
            # keep track of the last command sent
            if self._command != self._last_command:
                self._last_command = self._command
            
            await queue.publish(DroneControl(
                roll=self._command[0],
                pitch=self._command[1],
                yaw=self._command[2],
                throttle=self._command[3],
            ).to_rawdata()
            )
        except Exception as e:
            self.logerr(f"Error communicating with board {e}")

    # heartbeat callbacks: These update the last time that data was received from a node

    async def _heartbeat_joystick_cb(self, _):
        """ Update joystick heartbeat """
        self._heartbeat_joystick = time.time()

    async def _heartbeat_pid_cb(self, _):
        """ Update pid_controller heartbeat """
        self._heartbeat_pid = time.time()

    async def _heartbeat_altitude_cb(self, _):
        """ Update altitude sensor heartbeat """
        self._heartbeat_altitude = time.time()

    async def _heartbeat_state_estimator_cb(self, _):
        """ Update state_estimator heartbeat """
        self._heartbeat_state_estimator = time.time()

    def _should_disarm(self):
        """
        Disarm the drone if the battery values are too low or if there is a
        missing heartbeat
        """
        curr_time = time.time()
        disarm = False

        # - joystick
        if self.configuration.heartbeats.joystick and \
                curr_time - self._heartbeat_joystick > self._heartbeat_thr:
            self.logfatal("\nSafety Failure: not receiving flight commands. "
                          "Check the 'Joystick' node\n")
            disarm = True
        # - pid
        if self.configuration.heartbeats.pid and \
                curr_time - self._heartbeat_pid > self._heartbeat_thr:
            self.logfatal("\nSafety Failure: not receiving flight commands. "
                          "Check the 'PID' node\n")
            disarm = True
        # - altitude
        if self.configuration.heartbeats.altitude and \
                curr_time - self._heartbeat_altitude > self._heartbeat_thr:
            self.logfatal("\nSafety Failure: not receiving data from the IR sensor. "
                          "Check the 'altitude' node\n")
            disarm = True
        # - state_estimator
        if self.configuration.heartbeats.state_estimator and \
                curr_time - self._heartbeat_state_estimator > self._heartbeat_thr:
            self.logfatal("\nSafety Failure: not receiving a state estimate. "
                          "Check the 'state estimator' node\n")
            disarm = True

        return disarm

    def _read_motor_pwm_signals(self) -> DroneMotorCommand:
        """
        Reads the motor signals sent by the flight controller to the ESCs.
        """
        # create Motor message
        m1, m2, m3, m4 = self._board.motors_pwm

        return DroneMotorCommand(
            minimum=self.configuration.motor_command_range[0],
            maximum=self.configuration.motor_command_range[1],
            m1=m1,
            m2=m2,
            m3=m3,
            m4=m4,
        )

    def on_shutdown(self):
        """
        Disarm the drone and quits the flight controller node.
        """
        if self._board is None:
            return
        self.loginfo("Disarming!")
        self._command = self._board.mode_to_rc_command(DroneMode.DISARMED)
        self._switch_to_mode(DroneMode.DISARMED, quiet=True)
        # self._mode_pub.publish(DroneModeMsg(mode=DroneMode.DISARMED.value))
        sys.exit()


def main():
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True, help="Name of the configuration")
    args: argparse.Namespace = parser.parse_args()
    # create node
    node: FlightControllerNode = FlightControllerNode(config=args.config)
    # launch the node
    node.spin()


if __name__ == "__main__":
    main()
