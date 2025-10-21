import pickle
import sys
import time
from threading import Thread

import cv2
import qimage2ndarray
import numpy as np

from datainterface.qt_sock_stream_send import QSockStreamSend
from datainterface.video_recv import VideoRecv
from qt_sock_stream_recv import QSockStreamRecv
from typing import TYPE_CHECKING, Sequence, Union
from io import StringIO
import pygame

from PyQt6.QtCore import pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QImage

from rov_float_data_structures.float_data import FloatData
from rov_float_data_structures.rov_data import ROVData
from data_classes.vector3 import Vector3
from video_frame import VideoFrame
from data_classes.stdout_type import StdoutType
from numpy import ndarray

if TYPE_CHECKING:
    from window import Window
    from app import App


class DataInterface(QObject):
    rov_data_update = pyqtSignal()
    float_data_update = pyqtSignal()
    stdout_update = pyqtSignal(StdoutType, str)

    # ALERT SIGNALS
    attitude_alert = pyqtSignal()
    depth_alert = pyqtSignal()
    ambient_temperature_alert = pyqtSignal()
    ambient_pressure_alert = pyqtSignal()
    internal_temperature_alert = pyqtSignal()
    float_depth_alert = pyqtSignal()

    def __init__(self, app: "App", windows: Sequence["Window"], redirect_stdout: StringIO, redirect_stderr: StringIO,
                 video_feed_count=2):
        super().__init__()
        self.app: "App" = app
        self.windows: Sequence["Window"] = windows
        self.video_feed_count: int = video_feed_count

        self.redirect_stdout = redirect_stdout
        self.redirect_stderr = redirect_stderr

        self.attitude = Vector3(0, 0, 0)  # Pitch, Yaw, Roll
        self.angular_acceleration = Vector3(0, 0, 0)
        self.angular_velocity = Vector3(0, 0, 0)
        self.acceleration = Vector3(0, 0, 0)
        self.velocity = Vector3(0, 0, 0)
        self.depth = 0
        self.ambient_temperature = 0
        self.ambient_pressure = 0
        self.internal_temperature = 0
        self.cardinal_direction = 0
        self.grove_water_sensor = 0

        self.actuator_1 = 0
        self.actuator_2 = 0
        self.actuator_3 = 0
        self.actuator_4 = 0
        self.actuator_5 = 0
        self.actuator_6 = 0

        # MATE FLOAT Data
        self.float_depth: float = 0

        # STDOUT UI Thread
        # This thread processes redirected stdout to be displayed in the UI and in console
        self.stdout_ui_thread = Thread(target=self.f_stdout_ui_thread)
        self.stdout_ui_thread.start()

        # Camera Feeds

        self.camera_frames: [VideoFrame] = []
        self.video_threads: [QSockStreamRecv] = []

        # Controller State

        pygame.init()
        pygame.joystick.init()
        self.joystick = None

        self.debug_angle = 0.0
        self.last_debug_time = time.time()
        self.min_depth = 0.0
        self.max_depth = 5.0

        # Video Receiver Threads
        cam_index_offset = 0
        for video_feed_index in range(self.video_feed_count):
            # Get the config data for this video feed
            feed_config = self.app.feed_config[video_feed_index]
            port = self.app.port_bindings[f"feed_{video_feed_index}"]

            # A stereo video feed will produce two camera frames
            if feed_config["type"] == "stereo":
                self.camera_frames.append(VideoFrame())
            self.camera_frames.append(VideoFrame())

            # Create a receiver thread for this video feed
            cam_thread = VideoRecv(self.app, [self.app.ROV_IP, "127.0.0.1"][self.app.ROV_IP == "localhost"],
                                   port, video_feed_index)

            # Connect a signal to pass video feed data to the data-interface's handler
            cam_thread.on_recv.connect(
                lambda payload, cam=video_feed_index + cam_index_offset, feed=video_feed_index:
                self.on_video_stream_sock_recv(payload, cam, feed))

            # Connect a signal for when a video feed disconnects
            cam_thread.on_disconnect.connect(
                lambda cam=video_feed_index + cam_index_offset, feed=video_feed_index:
                self.on_camera_feed_disconnect(cam, feed))

            # Camera indexes will be offset from the video feed index by however many stereo feeds are connected
            if feed_config["type"] == "stereo":
                cam_index_offset += 1
            self.video_threads.append(cam_thread)
            cam_thread.start()

        # ROV Data Thread
        self.rov_data_thread = QSockStreamRecv(self.app, self.app.UI_IP, self.app.port_bindings["data"])
        self.rov_data_thread.on_recv.connect(self.on_rov_data_sock_recv)
        self.rov_data_thread.start()

        # ROV Float Thread
        self.float_data_thread = QSockStreamRecv(self.app, self.app.UI_IP, self.app.port_bindings["float_data"])
        self.float_data_thread.on_recv.connect(self.on_float_data_sock_recv)
        self.float_data_thread.start()

        # STDOUT Socket Thread
        # This thread processes stdout that has been received across a socket
        self.stdout_sock_thread = QSockStreamRecv(self.app, self.app.UI_IP, self.app.port_bindings["stdout"])
        self.stdout_sock_thread.on_recv.connect(self.on_stdout_sock_recv)
        self.stdout_sock_thread.start()

        # Controller Input Thread
        # Collects and sends input to the ROV
        print("Creating sock stream send")
        self.controller_input_thread = QSockStreamSend(self.app, self.app.ROV_IP, self.app.port_bindings["control"],
                                                       self.get_controller_input, 0.01)
        self.controller_input_thread.start()

        self.timer = QTimer(self)
        self.attitude_alert_once = False
        self.depth_alert_once = False
        self.ambient_temperature_alert_once = False
        self.ambient_pressure_alert_once = False
        self.internal_temperature_alert_once = False
        self.float_depth_alert_once = False

    def export_rov_data(self):
        data = ROVData()
        data.attitude = self.attitude
        data.angular_acceleration = self.angular_acceleration
        data.angular_velocity = self.angular_velocity
        data.acceleration = self.acceleration
        data.velocity = self.velocity
        data.depth = self.depth
        data.ambient_temperature = self.ambient_temperature
        data.ambient_pressure = self.ambient_pressure
        data.internal_temperature = self.internal_temperature
        data.cardinal_direction = self.cardinal_direction
        data.grove_water_sensor = self.grove_water_sensor

        return data

    def on_camera_feed_disconnect(self, cam: int, feed: int) -> None:
        feed_config = self.app.feed_config[feed]

        # Send disconnection signal to both camera frames if the video feed was providing stereo data
        if feed_config["type"] == "stereo":
            with self.camera_frames[cam + 1].lock:
                self.camera_frames[cam + 1].frame = None
                self.camera_frames[cam + 1].new_frame.emit()

        with self.camera_frames[cam].lock:
            self.camera_frames[cam].frame = None
            self.camera_frames[cam].new_frame.emit()

    def is_rov_connected(self) -> bool:
        return self.rov_data_thread.is_connected()

    def is_float_connected(self) -> bool:
        return self.float_data_thread.is_connected()

    def is_controller_connected(self) -> bool:
        return self.get_controller_input() is not None

    def on_rov_data_sock_recv(self, payload_bytes) -> None:
        rov_data: ROVData = pickle.loads(payload_bytes)
        for attr in rov_data.__dict__:
            setattr(self, attr, getattr(rov_data, attr))

        if not self.attitude_alert_once and (self.attitude.z > 4 or self.attitude.z < -5):
            self.attitude_alert_once = True
            self.attitude_alert.emit()

        if not self.depth_alert_once and (self.depth > 2.5 or self.depth < 1):
            self.depth_alert_once = True
            self.depth_alert.emit()

        if not self.ambient_temperature_alert_once and (
                self.ambient_temperature < 24 or self.ambient_temperature > 28):
            self.ambient_temperature_alert_once = True
            self.ambient_temperature_alert.emit()

        if not self.ambient_pressure_alert_once and self.ambient_pressure > 129:
            self.ambient_pressure_alert_once = True
            self.ambient_pressure_alert.emit()

        if not self.internal_temperature_alert_once and self.internal_temperature > 69:
            self.internal_temperature_alert_once = True
            self.internal_temperature_alert.emit()

        self.rov_data_update.emit()

    def on_float_data_sock_recv(self, payload_bytes: bytes) -> None:
        float_data: FloatData = pickle.loads(payload_bytes)
        for attr in float_data.__dict__:
            setattr(self, attr, getattr(float_data, attr))
        self.float_data_update.emit()

        if not self.float_depth_alert_once and (self.float_depth > 3 or self.float_depth < 1):
            self.float_depth_alert_once = True
            self.float_depth_alert.emit()

    def on_video_stream_sock_recv(self, frame: Union[ndarray, None], cam: int, feed: int) -> None:
        # Process the raw video bytes received
        if frame is None:
            print("disconnect")
            self.on_camera_feed_disconnect(cam, feed)
            return

        if not frame.flags['C_CONTIGUOUS']:
            frame = np.ascontiguousarray(frame)

        frame_h, frame_w, _ = frame.shape

        feed_config = self.app.feed_config[feed]

        if feed_config["type"] == "stereo":
            # Code for efficiently seperating video feed into the left and right cameras
            frame_w //= 2
            left_cam = frame[:, :frame_w]
            right_cam = frame[:, frame_w:]

            # Swap Red and Blue colour channels
            left_cam = left_cam[..., [2, 1, 0]]
            right_cam = right_cam[..., [2, 1, 0]]

            left_cam = qimage2ndarray.array2qimage(left_cam)
            right_cam = qimage2ndarray.array2qimage(right_cam)

            #  Wait until no other threads are accessing the VideoFrame for the Left Camera
            with self.camera_frames[cam].lock:
                self.camera_frames[cam].frame = left_cam
                self.camera_frames[cam].new_frame.emit()

            #  Wait until no other threads are accessing the VideoFrame for the Right Camera
            with self.camera_frames[cam + 1].lock:
                self.camera_frames[cam + 1].frame = right_cam
                self.camera_frames[cam + 1].new_frame.emit()

            return
        elif feed_config["type"] == "fisheye":
            # Load saved calibration data
            undistorted_frame = cv2.remap(frame, feed_config["map1"], feed_config["map2"],
                                          interpolation=cv2.INTER_LINEAR)

            # Crop the valid region
            x, y, w, h = feed_config["calibration_data"]["roi"]
            undistorted_frame = undistorted_frame[y:y + h, x:x + w]

            frame = undistorted_frame

        elif not feed_config["type"] == "default":
            print(f"Unrecognised camera feed type '{feed_config['type']}' in feed_config.json")
            return
        # Generate the new QImage for the feed
        frame = QImage(frame, frame_w, frame_h, QImage.Format.Format_BGR888)

        #  Wait until no other threads are accessing the VideoFrame
        with self.camera_frames[cam].lock:
            self.camera_frames[cam].frame = frame
            self.camera_frames[cam].new_frame.emit()

    def f_stdout_ui_thread(self) -> None:
        while not self.app.closing:
            time.sleep(0)
            # Process redirected stdout
            self.redirect_stdout.flush()
            self.redirect_stderr.flush()
            for redirect, source, type_ in ((self.redirect_stdout, sys.__stdout__, StdoutType.UI),
                                            (self.redirect_stderr, sys.__stderr__, StdoutType.UI_ERROR)):
                # If stdout has been redirected, send it to redirected and source location
                if redirect != source:
                    lines = redirect.getvalue().splitlines()
                    for line in lines:
                        self.stdout_update.emit(type_, line)
                        print(line, file=source)
                    # Clean up redirect buffer
                    redirect.seek(0)
                    redirect.truncate(0)

    def on_stdout_sock_recv(self, payload_bytes: bytes) -> None:
        # Receive stdout from socket
        try:
            lines: [tuple[StdoutType, str]] = pickle.loads(payload_bytes)
            for line in lines:
                source, line = line

                self.stdout_update.emit(source, line)

                print(f"[{source.name}] {line}",
                      file=(sys.__stdout__ if source != StdoutType.ROV_ERROR else sys.__stderr__))
        except ValueError:
            print("Received stdout was not of format <STDOUTTYPE>, <str>", file=sys.stderr)

    def get_controller_input(self) -> bytes:
        # Process all pygame events since the function was last called
        for event in pygame.event.get():
            # Handle Controller connection and disconnection
            if event.type == pygame.JOYDEVICEADDED:
                if self.joystick is None:
                    self.joystick = pygame.joystick.Joystick(0)
                    self.joystick.init()
                    print("Controller Connected", flush=True)
            elif event.type == pygame.JOYDEVICEREMOVED:
                self.joystick.quit()
                self.joystick = None
                print("Controller Disconnected", flush=True)
                return pickle.dumps(None)
        if self.joystick is None:
            return pickle.dumps(None)
        new_state = {
            "axes": [self.joystick.get_axis(i) for i in range(self.joystick.get_numaxes())],
            "buttons": [self.joystick.get_button(i) for i in range(self.joystick.get_numbuttons())],
            "hats": [self.joystick.get_hat(i) for i in range(self.joystick.get_numhats())]
        }
        return pickle.dumps(new_state)

    def close(self):
        if not self.app.closing:
            raise AssertionError("This function should only be called from App after App.closing is set to True")
        # Rejoin all threads
        pygame.quit()
        print("Joining ROV data thread", file=sys.__stdout__, flush=True)
        self.rov_data_thread.wait(10)
        print("Joining socket stdout thread", file=sys.__stdout__, flush=True)
        self.stdout_sock_thread.wait(10)
        print("Joining ui stdout thread", file=sys.__stdout__, flush=True)
        self.stdout_ui_thread.join(10)
        print("Joining controller thread", file=sys.__stdout__, flush=True)
        self.controller_input_thread.wait(10)
        print("Joining video stream threads", file=sys.__stdout__, flush=True)
        for video_stream_thread in self.video_threads:
            video_stream_thread.wait(10)
        print("Data Interface closed successfully", file=sys.__stdout__, flush=True)
