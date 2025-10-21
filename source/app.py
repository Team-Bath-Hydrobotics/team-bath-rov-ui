import json
import sys
from io import StringIO, TextIOWrapper

from threading import ThreadError
from typing import Union

import cv2
import numpy as np
from PyQt6.QtCore import pyqtSignal, QThread, Qt

from copilot.copilot import Copilot
from datainterface.data_interface import DataInterface
from dock import Dock
from grapher.grapher import Grapher
from pilot.pilot import Pilot
from tasks.task import Task

from PyQt6.QtWidgets import QApplication, QWidget

from window import Window


class App(QApplication):
    """
        Class for storing data about the overall application.
    """
    task_checked = pyqtSignal(QWidget)

    def __init__(self,
                 redirect_stdout: Union[StringIO, TextIOWrapper],
                 redirect_stderr: Union[StringIO, TextIOWrapper],
                 argv):
        try:
            with open("ui_config.json", "r") as f:
                self.ui_config = json.load(f)
        except FileNotFoundError:
            print("No ui_config.json file", file=sys.__stderr__)
            exit(1)
        except json.decoder.JSONDecodeError:
            print("Malformed ui_config.json file", file=sys.__stderr__)
            exit(1)
        try:
            local_test = self.ui_config["local_test"]
        except KeyError:
            print("ui_config.json doesn't specify `local_test` parameter - Defaulting to `False`", file=sys.__stderr__)
            local_test = True

        try:
            rov_ip = self.ui_config["rov_ip"]
        except KeyError:
            print("ui_config.json doesn't specify `rov_ip` parameter - Defaulting to `localhost`", file=sys.__stderr__)
            rov_ip = "localhost"

        try:
            float_ip = self.ui_config["float_ip"]
        except KeyError:
            print("ui_config.json doesn't specify `float_ip` parameter - Defaulting to `localhost`", file=sys.__stderr__)
            float_ip = "localhost"

        try:
            self.feed_config = self.ui_config["camera_data"]
        except KeyError:
            print("Couldn't find 'camera_data' in ui_config.json", file=sys.__stderr__)
            self.feed_config = []

        try:
            self.port_bindings = self.ui_config["port_bindings"]
        except KeyError:
            print("Couldn't find 'port_bindings' in ui_config.json", file=sys.__stderr__)
            exit(1)

        self.setStyle("Fusion")

        self.video_feed_count = len(self.feed_config)

        self.redirect_stdout = redirect_stdout
        self.redirect_stderr = redirect_stderr

        self.local_test = local_test
        if local_test:
            self.UI_IP = "localhost"
            self.ROV_IP = "localhost"
            self.FLOAT_IP = "localhost"
        else:
            self.UI_IP = "0.0.0.0"
            self.ROV_IP = rov_ip
            self.FLOAT_IP = float_ip

        for conf in self.feed_config:
            if conf["type"] == "fisheye":
                try:
                    conf["calibration_data"] = np.load(conf["undistort_file"])

                    for key in ["camera_matrix", "dist_coeffs", "new_camera_matrix", "roi"]:
                        if key not in conf["calibration_data"]:
                            raise KeyError()

                    new_camera_matrix, _ = cv2.getOptimalNewCameraMatrix(
                        conf["calibration_data"]["camera_matrix"],
                        conf["calibration_data"]["dist_coeffs"],
                        (conf["width"], conf["height"]), alpha=0
                    )

                    conf["map1"], conf["map2"] = cv2.initUndistortRectifyMap(
                        conf["calibration_data"]["camera_matrix"],
                        conf["calibration_data"]["dist_coeffs"],
                        None,
                        new_camera_matrix,
                        (conf["width"], conf["height"]),
                        cv2.CV_16SC2
                    )

                except FileNotFoundError:
                    print(f"Path to Feed {i} Undistort File is Invalid: `{conf['undistort_file']}`", file=sys.__stderr__)
                    exit(1)
                except KeyError:
                    for key in ["undistort_file", "width", "height"]:
                        if key not in conf:
                            print(f"Fisheye camera feeds must have '{key}' parameter", file=sys.__stderr__)
                    else:
                        print("Malformed Undistort File Provided", file=sys.__stderr__)
                    exit(1)

        ports = []
        for binding in ["data", "float_data", "stdout", "control", "power", "action"]:
            if binding not in self.port_bindings:
                raise ValueError(f"File is missing port for {binding} in ui_config.json")
            if self.port_bindings[binding] in ports:
                raise ValueError(f"Port {self.port_bindings[binding]} is bound to more than once in ui_config.json")
            ports.append(binding)
        for i in range(self.video_feed_count):
            key = f"feed_{i}"
            if key not in self.port_bindings:
                raise ValueError(f"Port {self.port_bindings[binding]} is bound to more than once in ui_config.json")

        super().__init__(argv)
        self.closing = False

        # TEMPORARY FOR PROCESS SIMULATION
        self.float_data_source_proc = None

        # Create the list of tasks the ROV should complete

        self.tasks: list[Task] = [
            Task(self, "Install AUV docking station", "Move the ROV around and get used to the controls"),
            Task(self, "Place probiotic irrigation system in designated location",
                 "Here is another task that you need to do!", (2, 30)),
            Task(self, "Swim in a cirlce", "Sounds fun!", (3, 30)),
            Task(self, "Produce model of the shipwreck", "Sounds fun!", (5, 45)),
            Task(self, "Do a backflip", "Hell yeah!", (8, 30)),
            Task(self, "Perform a magic trick", "", (10, 10)),
            Task(self, "Deploy the MATE float and move it to the required location", "Nice", (11, 30)),
            Task(self, "Recieve the first set of readings for submersion", "Cool This is very interesting stuff",
                 (12, 0)),
            Task(self, "Recieve the second set of readings for submersion", "Cool This is very interesting stuff",
                 (13, 30)),
            Task(self, "Return to the surface", "Rahhhh", (13, 0)),
            Task(self, "Return to base", "Quickly!", (14, 0))
        ]
        self.tasks = list(sorted(self.tasks, key=lambda t: t.start_time[0] * 60 + t.start_time[1], reverse=True))

        # Get all monitors connected to the computer

        # Assign each window to its own monitor if available
        pilot_monitor = 0
        copilot_monitor = 0
        graph_monitor = 0
        if len(self.screens()) > 1:
            copilot_monitor = 1
            graph_monitor = 1
        if len(self.screens()) > 2:
            graph_monitor = 2

        # Build the dock container

        self.dock = Dock(self, self.screens()[copilot_monitor])

        # Create windows

        self.copilot_window = Copilot(self, self.screens()[copilot_monitor])
        self.pilot_window = Pilot(self, self.screens()[pilot_monitor])
        self.grapher_window = Grapher(self, self.screens()[graph_monitor])

        # Attach the navigation bars to these windows

        self.copilot_window.attach_nav_bar(self.dock)
        self.pilot_window.attach_nav_bar(self.dock)
        self.grapher_window.attach_nav_bar(self.dock)

        # Add windows to the dock
        self.dock.add_windows(self.copilot_window, self.pilot_window, self.grapher_window)

        # Undock windows if extra monitors are available
        if len(self.screens()) > 1:
            self.pilot_window.nav.f_undock()

        if len(self.screens()) > 2:
            self.grapher_window.nav.f_undock()

        self.dock.showFullScreen()
        current_window: Window = self.dock.currentWidget()
        current_window.nav.clear_layout()
        current_window.nav.generate_layout()

        windows: list[Window] = [self.copilot_window, self.pilot_window, self.grapher_window]
        # Create the data interface
        # Local redirected stdout/stderr should be passed so that it can be processed in this thread.
        self.data_interface_thread = QThread()
        self.data_interface: DataInterface = DataInterface(self, windows, redirect_stdout, redirect_stderr,
                                                           self.video_feed_count)
        self.data_interface.moveToThread(self.data_interface_thread)
        self.data_interface_thread.start()

        # Connect windows to data interface signals
        for window in windows:
            window.attach_data_interface()

        # Initially set the rov/float to be disconnected
        self.data_interface.rov_data_thread.on_disconnect.emit()
        self.data_interface.float_data_thread.on_disconnect.emit()

        dark_theme = """
            Window {
                background-color:rgb(30,30,30);
            }

            QScrollArea#TaskList {
                background-color:rgb(50,50,50);
                color: white;
            }
            QWidget#TaskListContents {
                background-color:rgb(50,50,50);
                color: white;

            }
            QFrame {
                background-color:rgb(30,30,30);
                color: white;

            }
            QLabel {
                color: white;
            }
            QPlainTextEdit#Stdout {
                background-color:rgb(50,50,50);
                color: white;
            }
            QRadioButton {
                color:white;
            }
        """
        self.setStyleSheet(dark_theme)

        if not local_test:
            print("Note: RUN_ROV_LOCALLY is set to False in main.py - Only use this when testing with the "
                  "Raspberry Pi or for production")

    def reset_task_completion(self) -> None:
        for task in self.tasks:
            task.completed = False

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        print("Closing", file=sys.__stdout__, flush=True)
        # Close dummy processes
        if self.float_data_source_proc:
            print("Attempting to close Float data source", file=sys.__stdout__, flush=True)
            try:
                self.float_data_source_proc.terminate()
                print("Killed float data source", file=sys.__stdout__, flush=True)
            except Exception as e:
                print("Couldn't kill float data source - ", e, file=sys.__stdout__, flush=True)
        # Rejoin threads before closing
        try:
            self.data_interface.close()
            print("Closing data interface threads", file=sys.__stdout__, flush=True)
        except ThreadError as e:
            print("Could not close data interface threads.", file=sys.__stdout__, flush=True)
        self.quit()
