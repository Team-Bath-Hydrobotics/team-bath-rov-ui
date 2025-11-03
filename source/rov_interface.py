# This script creates a simulated version of the ROV that the UI can interact with
import io
import json
import os
import pickle
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from threading import Thread
import subprocess
import psutil

from data_classes.vector3 import Vector3

try:
    import serial
    import board
    import busio
    # sudo pip3 install python3-adafruit-circuitpython-bno055 --break-system-packages

    import adafruit_bno055
    import traceback
except ModuleNotFoundError as e:
    print("Raspberry Pi Modules Could not be located - likely because running locally", file=sys.stderr)
    print("Otherwise, ensure that all modules are installed:", e)
    

try:
    # Initialize I2C bus
    i2c = busio.I2C(board.SCL, board.SDA)

    # Initialize BNO055 sensor
    imu_sensor = adafruit_bno055.BNO055_I2C(i2c)

    # Optional: Set operation mode (default is NDOF which is good for orientation)
    # sensor.mode = adafruit_bno055.NDOF_MODE
except Exception as e:
    imu_sensor = None
    print("Couldn't connect to I2C for IMU sensor", e)

script_dir = os.path.dirname(os.path.abspath(__file__))  # Get the script's directory
os.chdir(script_dir)  # Change working directory to the script's location

from data_classes.action_enum import ActionEnum
from rov_float_data_structures.rov_data import ROVData
from data_classes.stdout_type import StdoutType
from datainterface.sock_stream_recv import SockStreamRecv
from datainterface.sock_stream_send import SockStreamSend, SockSend



thruster_matrix = [
    [0, 0, -0.707, 0.707, 0.707, -0.707],  # x
    [0, 0, 0.707, 0.707, 0.707, 0.707],  # y
    [1, 1, 0, 0, 0, 0],  # z
    [0, 0, -0.0707, -0.0707, 0.0707, -0.0707],  # roll
    [-0.13, -0.13, -0.0707, -0.0707, -0.0707, -0.0707],  # yaw
    [0, 0, -0.182, 0.182, -0.182, 0.182]  # pitch
]


# Available Port Numbers: 49152-65535
class ROVInterface:
    def __init__(self, redirected_stdout, redirected_stderr, ui_ip=None, rov_ip=None, local_test=True, camera_data=None, port_bindings=None,
                 uart_port='/dev/ttyAMA0', uart_baud=115200, controller_test=False, data_poll=0.1, imu_sensor=None, show_camera_stdout=True):
        if camera_data is None:
            camera_data = []
        if port_bindings is None:
            port_bindings = {}
        self.local_test = local_test
        self.controller_test = controller_test
        self.camera_data = camera_data
        self.port_bindings = port_bindings
        self.camera_count = len(camera_data)
        self.redirected_stdout = redirected_stdout
        self.redirected_stderr = redirected_stderr
        self.show_camera_stdout = show_camera_stdout

        # Sensors/Arduino Connections

        self.data_poll = data_poll
        self.uart_port = uart_port
        self.uart_baud = uart_baud
        self.uart = None
        self.imu_sensor = imu_sensor

        if self.imu_sensor is None:
            print("No IMU Sensor Detected")

        # ROV state attributes

        self.closing = False
        self.closed = False
        self.hold_depth = 0
        self.maintain_depth = False
        self.next_send_controller_data = time.time()

        if self.local_test:
            self.UI_IP = "localhost"
            self.ROV_IP = "localhost"
        else:
            self.UI_IP = ui_ip
            self.ROV_IP = rov_ip

        if self.UI_IP is None:
            raise ValueError(
                "Please set ui_ip parameter in rov_config.json of ROVInterface to the IP of the device you would like to connect to.")

        if self.ROV_IP is None:
            raise ValueError(
                "Please set rov_ip parameter in rov_config.json of ROVInterface to the IP of the ROV")

        print(
            f"Creating ROV Interface:\n{self.UI_IP=}\n{self.ROV_IP=}\n{self.local_test=}\n{self.camera_count=}")

        self.rov_data = ROVData()
        self.i = 100  # temp variable

        print("Powering On...")

        print(f"Binding Data Thread to {self.UI_IP} : {self.port_bindings['data']}")

        self.data_thread = SockStreamSend(self, self.UI_IP, self.port_bindings["data"], self.data_poll,
                                          self.get_rov_data,
                                          on_connect=lambda: print("Data Thread Connected"),
                                          on_disconnect=lambda: print("Data Thread Disconnected")
                                          )
        self.data_thread.start()

        self.data_poll_thread = Thread(target=self.poll_rov_data)
        self.data_poll_thread.start()

        self.stdout_thread = SockStreamSend(self, self.UI_IP, self.port_bindings["stdout"], 0,
                                            self.process_stdout,
                                            on_connect=lambda: print("Stdout Thread Connected"),
                                            on_disconnect=lambda: print("Stdout Thread Disconnected")
                                            )
        self.stdout_thread.start()

        self.video_streams = None
        self.video_processes: [subprocess.Popen | None] = [None for _ in range(self.camera_count)]

        self.video_threads = []
        for i in range(self.camera_count):
            print("Binding video threads")
            port = self.port_bindings[f"feed_{i}"]
            print(f"\tBinding to {'127.0.0.1' if self.local_test else self.UI_IP} : {port}")
            video_thread = Thread(target=self.video_send,
                                  kwargs={"addr": "127.0.0.1" if self.local_test else self.UI_IP,
                                          "port": port, "i": i})
            video_thread.start()

            self.video_threads.append(video_thread)

        print(f"Binding Input Thread to {self.ROV_IP} : {self.port_bindings['control']}")

        self.input_thread = SockStreamRecv(self, self.ROV_IP, self.port_bindings["control"], self.controller_input_recv,
                                           on_connect=lambda: print("Controller Input Thread Connected"),
                                           on_disconnect=lambda: print("Controller Input Thread Disconnected")
                                           )
        self.input_thread.start()

        print(f"Binding Action Thread to {self.ROV_IP} : {self.port_bindings['action']}")

        self.action_thread = SockStreamRecv(self, self.ROV_IP, self.port_bindings["action"], self.action_recv,
                                            on_connect=lambda: print("Action Thread Connected"),
                                            on_disconnect=lambda: print("Action Thread Disconnected"))
        self.action_thread.start()

        print("Powered On!")

    def process_stdout(self):
        # Process redirected stdout
        payload = []
        while not self.closing and len(payload) == 0:
            time.sleep(0)
            redirected_stdout_io.flush()
            redirected_stderr_io.flush()

            for redirect, source, type_ in ((redirected_stdout_io, sys.__stdout__, StdoutType.ROV),
                                               (redirected_stderr_io, sys.__stderr__, StdoutType.ROV_ERROR)):
                # If stdout has been redirected, send it to redirected and source location
                if redirect != source:
                    lines = redirect.getvalue().splitlines()
                    for line in lines:
                        payload.append((type_, line))
                        print(line, file=source)
                    # Clean up redirect buffer
                    redirect.seek(0)
                    redirect.truncate(0)
            return pickle.dumps(payload)

    def get_rov_data(self) -> bytes:
        return pickle.dumps(self.rov_data)

    def poll_rov_data(self) -> None:
        delta_time = 0
        while not self.closing:
            st = time.time()
            self.rov_data.randomise()

            self.rov_data.ambient_pressure = self.i
            self.i += 0.1
            self.i %= 50
            self.i += 100

            if self.imu_sensor is not None:
                try:
                    euler = self.imu_sensor.euler
                    acceleration = self.imu_sensor.acceleration
                    gyro = self.imu_sensor.gyro
                    temp = self.imu_sensor.temperature
                    if euler is not None:
                        self.rov_data.attitude = Vector3(*euler)
                    if acceleration is not None:
                        self.rov_data.acceleration = Vector3(*acceleration) + Vector3(0, 0, -9)
                        self.rov_data.velocity += self.rov_data.acceleration * delta_time
                    if gyro is not None:
                        self.rov_data.angular_acceleration = Vector3(*gyro)
                        self.rov_data.angular_velocity += Vector3(*gyro) * delta_time
                    if temp is not None:
                        self.rov_data.internal_temperature = temp
                except OSError:
                    pass
                except Exception as e:
                    traceback.print_exception(e, file=sys.stderr)

            # if self.rov_data.attitude.x < -180:
            #     self.rov_data.attitude.x = 360 + self.rov_data.attitude.x
            # if self.rov_data.attitude.x > 180:
            #     self.rov_data.attitude.x = -(360 + self.rov_data.attitude.x)
            #
            # if self.rov_data.attitude.y < -180:
            #     self.rov_data.attitude.y = 360 + self.rov_data.attitude.y
            # if self.rov_data.attitude.y > 180:
            #     self.rov_data.attitude.y = -(360 + self.rov_data.attitude.y)
            #
            # if self.rov_data.attitude.z < -180:
            #     self.rov_data.attitude.z = 360 + self.rov_data.attitude.z
            # if self.rov_data.attitude.z > 180:
            #     self.rov_data.attitude.z = -(360 + self.rov_data.attitude.z)

            self.rov_data.depth += 0.01
            self.rov_data.depth %= 5

            if self.maintain_depth:
                self.rov_data.depth = self.hold_depth

            delta_time = time.time() - st
            sleep_time = delta_time
            if sleep_time < 0:
                sleep_time = 0
            elif sleep_time > self.data_poll:
                sleep_time = self.data_poll
            time.sleep(sleep_time)

    def kill_video_process(self, i):
        process = self.video_processes[i]
        if process is not None:
            try:
                process = psutil.Process(process.pid)
                for child in process.children(recursive=True):
                    child.terminate()
                process.terminate()
            except psutil.NoSuchProcess:
                pass

        self.video_processes[i] = None

    def controller_input_recv(self, payload_bytes: bytes) -> None:
        # Keep this code as it is!!!
        controller_data = pickle.loads(payload_bytes)
        if self.controller_test:

            
            if time.time() - self.next_send_controller_data > 0.1:
                print(f"{time.time():.2f} Controller Input:", controller_data)
                self.next_send_controller_data = time.time()
                
            return

        # Change the code here to modify how the controller data is recieved

        global thruster_matrix
        if controller_data is None:
            return

        if self.uart is None or not self.uart.is_open:
            try:
                self.uart = serial.Serial(self.uart_port, self.uart_baud, timeout=1)
                print(f"✅ UART opened on {self.uart_port} at {self.uart_baud} baud.")
            except Exception as e:
                print(f"❌ Failed to open UART: {e}")
                return

        axes = controller_data.get('axes', [])
        hats = controller_data.get('hats', [])
        print("Received Controller Data:")
        print("Axes:   ", axes)
        print("Buttons:", controller_data.get('buttons', []))
        print("Hats:   ", hats)
        print("-" * 40)

        # Get up to 4 axes, pad with zeros if not enough
        x = float(axes[0]) if len(axes) > 0 else 0.0
        z = float(axes[1]) if len(axes) > 1 else 0.0
        roll = float(axes[2]) if len(axes) > 2 else 0.0
        pitch = float(axes[3]) if len(axes) > 3 else 0.0

        # Clamp all axes to [-1, 1]
        x = max(-1.0, min(1.0, x))
        z = max(-1.0, min(1.0, z))
        roll = max(-1.0, min(1.0, roll))
        pitch = max(-1.0, min(1.0, pitch))

        # Input vector for thruster mixing: [x, y, z, roll, yaw, pitch]
        # y and yaw are unused, so set to 0.0
        axes_in = [x, 0.0, z, roll, 0.0, pitch]

        # Matrix multiplication: thruster = T * axes_in
        thruster = []
        for i in range(6):
            value = sum(thruster_matrix[j][i] * axes_in[j] for j in range(6))
            thruster.append(value)

        # Format for ESP32: "val1,val2,val3,val4,val5,val6\n"
        data_str = ','.join(f"{v:.4f}" for v in thruster) + '\n'
        if self.uart.is_open:
            self.uart.write(data_str.encode())
        else:
            print("Serial Connection to ESP32 Closed Unexpectedly")

    def action_recv(self, payload_bytes: bytes) -> None:
        print("Action Received")
        action = pickle.loads(payload_bytes)
        args = tuple()
        print(action)
        if type(action) is tuple:
            action, *args = action
        if action == ActionEnum.REINIT_CAMS:
            for i in range(self.camera_count):
                self.kill_video_process(i)
            print(f"Camera Feeds Re-initialised")
        elif action == ActionEnum.MAINTAIN_ROV_DEPTH:
            self.maintain_depth = args[0]

            if self.maintain_depth:
                print(f"Maintaining Depth At {args[1]:.2f} m")
                self.hold_depth = args[1]
            else:
                print("No Longer Maintaining Depth")
        elif action == ActionEnum.POWER_OFF_ROV:
            print("Closing")
            self.close()

    def close(self):
        print("Closing ROV Interface")
        self.closing = True
        print("Turning Off...")
        try:
            if self.action_thread.is_alive():
                self.action_thread.join(10)
        except Exception as e:
            print("Exception raised when closing Action Thread:", e, file=sys.stderr)
        print("Closed Action Thread")

        try:
            if self.input_thread.is_alive():
                self.input_thread.join(10)
        except Exception as e:
            print("Exception raised when closing Input Thread:", e, file=sys.stderr)
        print("Closed Input Thread")

        for video_thread in self.video_threads:
            try:
                if video_thread.is_alive():
                    video_thread.join(10)
            except Exception as e:
                print("Exception raised when closing Video Thread:", e, file=sys.stderr)
        print("Closed Video Threads")

        for i in range(self.camera_count):
            self.kill_video_process(i)
        print("Closed Video Processes")

        try:
            if self.data_thread.is_alive():
                self.data_thread.join(10)
        except Exception as e:
            print("Exception raised when closing Data Thread:", e, file=sys.stderr)
        print("Closed Data Thread")

        try:
            if self.data_poll_thread.is_alive():
                self.data_poll_thread.join(10)
        except Exception as e:
            print("Exception raised when closing Data Poll Thread:", e, file=sys.stderr)
        print("Closed Data Poll Thread")

        print("Closed")
        self.closed = True

    def video_send(self, addr: str, port: int, i: int) -> None:
        first = True
        while not self.closing:
            if not first:
                print(f"Restarting Video Process on {addr}:{port}")
            else:
                print(f"Starting Video Process on {addr}:{port}")
            first = False
            self.kill_video_process(i)
            time.sleep(1)
            if self.local_test:
                if os.name == "nt":
                    process = (f'ffmpeg -fflags nobuffer -f dshow -i video="{camera_devices[i]}" '
                               '-b:v 2M -preset ultrafast -tune zerolatency'
                               f' -r 24 -s 1920x1080 -f mpegts udp://{addr}:{port}')

                elif os.name == "posix":
                    process = (f'ffmpeg -f avfoundation -i "{i}" -c:v libx264 '
                               '-b:v 4M -preset ultrafast -tune zerolatency -g 30 '
                               f'-preset ultrafast -f mpegts udp://{addr}:{port}')
                else:
                    print("Warning: Detected you are not running on Windows or Mac.\n"
                          "If you are running this on the Raspberry PI, please set local_test to False",
                          file=sys.stderr)
                    process = (f"rpicam-vid -t {i} -n --width 1920 --height 1080"
                               f" --codec libav --libav-format mpegts"
                               f" --bitrate 30000000"
                               f" -o udp://{addr}:{port}")
            else:
                process = (f"rpicam-vid -t 0 --camera {i} -n "
                           f" --width {self.camera_data[i]['width']} --height {self.camera_data[i]['height']}"
                           f" --codec libav --libav-format mpegts"
                           f" --bitrate {self.camera_data[i]['bitrate']}"
                           f" --framerate {self.camera_data[i]['framerate']}"
                           f" --intra {self.camera_data[i]['framerate']}"
                           " --low-latency"
                           f" -o udp://{addr}:{port}")
            print(process)
            if self.show_camera_stdout:
                process = subprocess.Popen(process, shell=True)
                
            else:
                process = subprocess.Popen(process, shell=True,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       stdin=subprocess.PIPE)
            self.video_processes[i] = process
            time.sleep(1)
            while process.poll() is None and not self.closing:
                pass
            print("Video Process Died")


try:
    stderr_io = io.StringIO()
    with redirect_stderr(stderr_io) as redirected_stderr_io:
        stdout_io = io.StringIO()
        with redirect_stdout(stdout_io) as redirected_stdout_io:
            with open("rov_config.json", "r") as f:
                config_file = json.load(f)

            if os.name == "nt":
                print("Running rov_interface on Windows - Assuming local")
                from pygrabber.dshow_graph import FilterGraph

                graph = FilterGraph()
                camera_devices = graph.get_input_devices()

                config_file["local_test"] = True

            print(config_file)

            interface = ROVInterface(redirected_stdout_io, redirected_stderr_io, **config_file, imu_sensor=imu_sensor)

            while not interface.closed:
                time.sleep(0)


except FileNotFoundError:
    print((
        "Please create rov_config.json to the specification in ROV_INTERFACE_INSTALL.md.\nThis file should look like:\n"
        "{\n"
        '\t"ui_ip": <YOUR LAPTOP\'s IP>",\n'
        '\t"local_test": false,\n'
        '\t"camera_count": 3\n'
        '}'),
        file=sys.stderr)
except json.decoder.JSONDecodeError:
    print("Malformed rov_config.json file", file=sys.stderr)
