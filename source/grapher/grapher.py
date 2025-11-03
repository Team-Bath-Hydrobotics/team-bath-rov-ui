import math
import os
import pickle
import sys
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from PyQt6.QtCore import QThreadPool, QUrl, Qt, QSizeF, QTimer, QFileSystemWatcher, pyqtSignal, QEvent
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from clickable_label import ClickableLabel


from PyQt6.QtWidgets import QApplication, QVBoxLayout, QPushButton, QLineEdit, QMessageBox, QLabel, QTabWidget, \
    QFileDialog, QProgressBar, QTableWidget, QTableWidgetItem, QGraphicsView, QGraphicsScene, QFrame, QInputDialog, \
    QCheckBox, QComboBox, QWidget, QTextEdit, QSlider
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QImage
from PyQt6.QtMultimedia import QMediaPlayer

from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar

from grapher.graph_widget import GraphWidget
from grapher.photo_sphere_viewer import PhotosphereViewer
from grapher.stereo_depth_estimation import StereoVisionPipeline
from multi_select_widget import MultiSelectWidget
from rov_float_data_structures.float_data import FloatData
from rov_float_data_structures.rov_data import ROVData
from data_classes.vector3 import Vector3 as Vector3
from grapher.graphGenerator import GraphGenerator

from graphing_task import GraphingTask
from grapher.eDNASampler import eDNASampler
from nav_bar.nav_bar import NavBar

from window import Window

path_dir = os.path.dirname(os.path.realpath(__file__))


class Grapher(Window):
    recording_update = pyqtSignal()
    recording_end = pyqtSignal()
    photosphere_creation_progress_update = pyqtSignal(int)
    photosphere_creation_complete = pyqtSignal(str)

    def __init__(self, *args):
        super().__init__(os.path.join(path_dir, "grapher.ui"), *args)

        # General Attributes
        self.tab_widget: QTabWidget = self.findChild(QTabWidget, "TabWidget")

        # Graph Creator Attributes
        self.graph_generator = GraphGenerator()  # From graphGenerator.py
        self.toolbar = None
        self.generating_model = False
        self.displaying_no_graph_tab = False

        self.GraphTabWidget: QTabWidget = self.findChild(QTabWidget, "Graphs")
        self.GraphTabWidget.tabCloseRequested.connect(self.graph_closed)
        while self.GraphTabWidget.count() > 0:
            self.GraphTabWidget.removeTab(0)
        self.graph_closed()

        self.RecordedFieldsContainer: QFrame = self.findChild(QFrame, "RecordedFieldsContainer")

        self.StartRecordingButton: QPushButton = self.findChild(QPushButton, "StartRecording")
        self.StartRecordingButton.clicked.connect(self.toggle_recording)

        self.InputRecordingContainer: QFrame = self.findChild(QFrame, "InputRecordingContainer")
        self.InputRecordingComboContainer: QFrame = self.findChild(QFrame, "InputRecordingComboContainer")
        self.InputRecording = MultiSelectWidget(self.InputRecordingContainer, single_selection=True, scroll_height=50)
        self.InputRecordingComboContainer.layout().addWidget(self.InputRecording)
        self.InputRecording.item_selection_changed.connect(self.display_field_axes_options)

        self.LiveGraph: QCheckBox = self.findChild(QCheckBox, "LiveGraph")
        self.LiveGraph.setEnabled(False)

        def live_graph_change(state):
            if state == Qt.CheckState.Checked:
                self.InputRecording.set_checkboxes_enabled(False)
            else:
                self.InputRecording.set_checkboxes_enabled(True)

            self.display_field_axes_options()

        self.LiveGraph.checkStateChanged.connect(live_graph_change)

        self.XAxisSelector: QFrame = self.findChild(QFrame, "XAxisSelector")
        self.XAxisComboContainer: QFrame = self.findChild(QFrame, "XAxisComboContainer")
        self.XAxis = MultiSelectWidget(self.XAxisComboContainer, single_selection=True, scroll_height=50)
        self.XAxisComboContainer.layout().addWidget(self.XAxis)

        self.YAxisSelector: QFrame = self.findChild(QFrame, "YAxisSelector")
        self.YAxisComboContainer: QFrame = self.findChild(QFrame, "YAxisComboContainer")
        self.YAxis = MultiSelectWidget(self.YAxisComboContainer, single_selection=True, scroll_height=50)
        self.YAxisComboContainer.layout().addWidget(self.YAxis)

        self.ZAxisSelector: QFrame = self.findChild(QFrame, "ZAxisSelector")
        self.ZAxisComboContainer: QFrame = self.findChild(QFrame, "ZAxisComboContainer")
        self.ZAxis = MultiSelectWidget(self.ZAxisComboContainer, single_selection=True, scroll_height=50)
        self.ZAxisComboContainer.layout().addWidget(self.ZAxis)

        self.GraphTypeContainer: QFrame = self.findChild(QFrame, "GraphTypeContainer")
        self.GraphType = MultiSelectWidget(self.GraphTypeContainer, ["2D Graph", "3D Graph"], single_selection=True)

        def graph_type_logic():
            selected = self.GraphType.get_selected_display_text()
            if selected == "3D Graph":
                self.ZAxisSelector.setEnabled(True)
            elif selected == "2D Graph":
                self.ZAxisSelector.setEnabled(False)
            else:
                self.GraphType.set_selected_item("2D Graph")

        self.GraphType.item_selection_changed.connect(graph_type_logic)
        self.GraphType.set_selected_item("2D Graph")
        self.GraphTypeContainer.layout().addWidget(self.GraphType)

        self.CreateGraphButton: QPushButton = self.findChild(QPushButton, "CreateNewGraph")
        self.CreateGraphButton.clicked.connect(self.create_graph)

        self.is_recording = False
        self.recording_file_path = ""
        self.recording_dataframe: pd.DataFrame = None
        self.fields_to_record = []
        self.recording_timer = QTimer()
        self.recording_timer.timeout.connect(self.record_fields)
        self.recordings_path = Path(os.getcwd()) / "Recordings"

        if not self.recordings_path.exists():
            os.mkdir(self.recordings_path)

        self.on_recording_directory_changed()

        self.watcher = QFileSystemWatcher()
        self.watcher.addPath(str(self.recordings_path))
        self.watcher.directoryChanged.connect(self.on_recording_directory_changed)

        fields = []
        rov_data_obj = ROVData()
        for attr in rov_data_obj.__dict__:
            val = getattr(rov_data_obj, attr)
            if type(val) is Vector3:
                fields.append(attr + ".x")
                fields.append(attr + ".y")
                fields.append(attr + ".z")
            else:
                fields.append(attr)

        float_data_obj = FloatData()
        for attr in float_data_obj.__dict__:
            val = getattr(float_data_obj, attr)
            if type(val) == Vector3:
                fields.append(attr + ".x")
                fields.append(attr + ".y")
                fields.append(attr + ".z")
            else:
                fields.append(attr)

        self.RecordedFields = MultiSelectWidget()
        self.RecordedFieldsContainer.layout().addWidget(self.RecordedFields)
        self.RecordedFields.item_selection_changed.connect(self.display_field_axes_options)

        self.recordable_fields = {}

        for field in fields:
            pretty_field = field.replace("_", " ").replace(".", " ").title()
            self.recordable_fields[pretty_field] = field
            self.RecordedFields.add_item(pretty_field, field)

        # eDNA Attributes
        self.eDNA_database = None
        self.unknown_sample_folder = None

        self.eDNAResults: QTableWidget = self.findChild(QTableWidget, "eDNAResults")

        self.eDNAButton: QPushButton = self.findChild(QPushButton, "eDNAButton")
        self.eDNAButton.clicked.connect(self.on_eDNA_clicked)

        self.eDNADatabaseSelect: QPushButton = self.findChild(QPushButton, "eDNADatabaseSelect")
        self.eDNADatabaseSelect.clicked.connect(
            lambda: self.select_eDNA_path("eDNA_database", "eDNADatabasePath", "Choose A Folder of Known eDNA Samples"))

        self.eDNADatabasePath = self.findChild(QLabel, "eDNADatabasePath")

        self.UnknownSampleFolderSelect = self.findChild(QPushButton, "UnknownSampleFolderSelect")
        self.UnknownSampleFolderSelect.clicked.connect(
            lambda: self.select_eDNA_path("unknown_sample_folder", "UnknownSampleFolderPath",
                                          "Choose A Folder of Unknown eDNA Samples"))

        self.UnknownSampleFolderPath = self.findChild(QLabel, "UnknownSampleFolderPath")

        self.eDNAProgressBar: QProgressBar = self.findChild(QProgressBar, "eDNAProgress")

        self.eDNA_sampler = eDNASampler()
        if self.eDNA_sampler.build_success:
            self.eDNA_sampler.progress_update.connect(lambda pi: self.eDNAProgressBar.setValue(pi))
        else:
            self.eDNAButton.setDisabled(True)

        # Migration Attributes
        self.area1 = self.findChild(QLineEdit, "Area1Year")
        self.area2 = self.findChild(QLineEdit, "Area2Year")
        self.area3 = self.findChild(QLineEdit, "Area3Year")
        self.area4 = self.findChild(QLineEdit, "Area4Year")
        self.area5 = self.findChild(QLineEdit, "Area5Year")

        self.MigrationModelButton = self.findChild(QPushButton, "MigrationModelButton")
        self.MigrationModelButton.clicked.connect(self.on_generate_model_clicked)

        self.VideoContainer: QGraphicsView = self.findChild(QGraphicsView, "VideoContainer")
        self.VideoContainer.mousePressEvent = lambda event: self.play_migration_video()

        self.migration_media_player = QMediaPlayer()
        self.migration_video_item = QGraphicsVideoItem()
        self.migration_media_player.setVideoOutput(self.migration_video_item)

        scene = QGraphicsScene()
        scene.addItem(self.migration_video_item)

        self.VideoContainer.setScene(scene)

        # Photosphere Attributes

        self.PhotosphereOpenGLContainer: QFrame = self.findChild(QFrame, "PhotosphereOpenGLContainer")
        self.photosphere_viewer = PhotosphereViewer()
        self.PhotosphereOpenGLContainer.layout().addWidget(self.photosphere_viewer)
        self.photosphere_viewer.set_image_path("panorama_test.jpg")

        self.PhotosphereCameraSelectContainer: QFrame = self.findChild(QFrame, "PhotosphereCameraSelectContainer")

        self.PhotosphereCameraSelect = MultiSelectWidget(self.PhotosphereCameraSelectContainer,
                                                         single_selection=True,
                                                         scroll_height=50)
        self.PhotosphereCameraSelectContainer.layout().addWidget(self.PhotosphereCameraSelect)

        self.PhotospherePicture: QPushButton = self.findChild(QPushButton, "PhotospherePicture")
        self.PhotospherePicture.clicked.connect(self.take_picture)

        self.chosen_photosphere_directory = Path(os.getcwd()) / "Photosphere_Image_Data"

        if not self.chosen_photosphere_directory.exists():
            self.chosen_photosphere_directory.mkdir()

        self.PhotospherePictureDirectoryButton: QPushButton = self.findChild(QPushButton,
                                                                             "PhotospherePictureDirectoryButton")
        self.PhotospherePictureDirectoryButton.clicked.connect(self.change_photosphere_directory)

        self.PhotospherePictureDirectory: QLabel = self.findChild(QLabel, "PhotospherePictureDirectory")
        self.PhotospherePictureDirectory.setText(str(self.chosen_photosphere_directory))

        self.CreatePhotosphereButton: QPushButton = self.findChild(QPushButton, "CreatePhotosphereButton")
        self.CreatePhotosphereButton.clicked.connect(self.on_create_photosphere_clicked)

        self.PhotosphereCreationProgress: QProgressBar = self.findChild(QProgressBar, "PhotosphereCreationProgress")

        self.photosphere_creation_progress_update.connect(lambda value: self.PhotosphereCreationProgress.setValue(value))

        self.ResetPhotosphere: QPushButton = self.findChild(QPushButton, "ResetPhotosphere")
        self.ResetPhotosphere.clicked.connect(lambda: self.photosphere_viewer.reset_rotation())

        self.LoadPhotosphere: QPushButton = self.findChild(QPushButton, "LoadPhotosphere")
        self.LoadPhotosphere.clicked.connect(self.load_photosphere)

        self.photosphere_creation_complete.connect(self.photosphere_viewer.set_image_path)

        # Distance Estimation

        self.stereo_pipeline = StereoVisionPipeline()

        self.LeftStereoImageContainer: QFrame = self.findChild(QFrame, "LeftStereoImageContainer")
        self.LeftStereoImage = ClickableLabel(self.LeftStereoImageContainer)
        self.LeftStereoImage.setScaledContents(True)
        self.LeftStereoImageContainer.layout().addWidget(self.LeftStereoImage)

        self.RightStereoImageContainer: QFrame = self.findChild(QFrame, "RightStereoImageContainer")
        self.RightStereoImage = ClickableLabel(self.RightStereoImageContainer)
        self.RightStereoImage.setScaledContents(True)
        self.RightStereoImageContainer.layout().addWidget(self.RightStereoImage)

        self.LeftStereoImage.clicked.connect(lambda x, y: self.stereo_pipeline.add_point(x, y))

        self.DisparityMap: QLabel = self.findChild(QLabel, "DisparityMap")
        self.stereo_pipeline.new_disparity_map.connect(
            lambda: (
                self.DisparityMap.setPixmap(QPixmap.fromImage(
                    QImage(
                        self.stereo_pipeline.disparity_map_display.tobytes(),
                        self.stereo_pipeline.disparity_map_display.shape[1],
                        self.stereo_pipeline.disparity_map_display.shape[0],
                        QImage.Format.Format_Grayscale8
                        )
                ))
                , print("New Disparity Map")
            )
        )

        self.ResetPoints: QPushButton = self.findChild(QPushButton, "ResetStereoPoints")
        self.ResetPoints.clicked.connect(lambda: (self.stereo_pipeline.reset_points(), self.draw_corresponding_points()))

        self.stereo_pipeline.load_images("Stereo_Images/L_0.png", "Stereo_Images/R_0.png")
        self.stereo_pipeline.corresponding_point_found.connect(self.draw_corresponding_points)
        self.draw_corresponding_points()

        self.FocalSlider: QSlider = self.findChild(QSlider, "FocalSlider")
        self.FocalTextEdit: QLineEdit = self.findChild(QLineEdit, "FocalInput")

        self.FocalTextEdit.textEdited.connect(
            lambda text: (
                self.FocalSlider.setValue(int(text)),
                self.stereo_pipeline.__setattr__("focal_length", int(text))
            ) if text.isnumeric()
            else None

        )
        self.FocalSlider.valueChanged.connect(
            lambda value: (
                self.FocalTextEdit.setText(str(value)),
                self.stereo_pipeline.__setattr__("focal_length", value)
            )
        )
        self.FocalSlider.setValue(700)

        self.BaselineSlider: QSlider = self.findChild(QSlider, "BaselineSlider")
        self.BaselineTextEdit: QLineEdit = self.findChild(QLineEdit, "BaselineInput")

        self.BaselineTextEdit.textEdited.connect(
            lambda text: self.BaselineSlider.setValue(int(text)) if text.isnumeric()
            else None

        )
        self.BaselineSlider.valueChanged.connect(
            lambda value: (
                self.BaselineTextEdit.setText(str(value)),
                self.stereo_pipeline.__setattr__("baseline", value/1000)
            )
        )
        self.BaselineSlider.setValue(100)

        self.P1CorrespondingScore: QLabel = self.findChild(QLabel, "P1CorrespondingScore")
        self.P1CorrespondingRating: QLabel = self.findChild(QLabel, "P1CorrespondingRating")
        self.P1Disparity: QLabel = self.findChild(QLabel, "P1Disparity")
        self.P13DPosition: QLabel = self.findChild(QLabel, "P13DPosition")

        self.P2CorrespondingScore: QLabel = self.findChild(QLabel, "P2CorrespondingScore")
        self.P2CorrespondingRating: QLabel = self.findChild(QLabel, "P2CorrespondingRating")
        self.P2Disparity: QLabel = self.findChild(QLabel, "P2Disparity")
        self.P23DPosition: QLabel = self.findChild(QLabel, "P23DPosition")

        self.DistanceResult: QLabel = self.findChild(QLabel, "DistanceResult")

        self.stereo_pipeline.new_result.connect(self.update_distance_estimation_results)


    def update_distance_estimation_results(self):
        r = self.stereo_pipeline.results

        results = {
            'distance_3d': None,
            'point1_3d': None,
            'point2_3d': None,
            'depths': None,
        }

        self.P1CorrespondingScore.setText(
        "N/A" if r["point1_confidence_score"] is None else f"{r['point1_confidence_score']:.3f}")
        self.P1CorrespondingRating.setText(
        "N/A" if r["point1_confidence_rating"] is None else r["point1_confidence_rating"]
        )
        self.P1Disparity.setText("N/A" if r["disparity"] is None else f"{r['disparity'][0]:.3f}")
        self.P13DPosition.setText(
            "N/A" if r["point1_3d"] is None else f"{','.join(['{a:.3f}' for a in r['point1_3d']])}")

        self.P2CorrespondingScore.setText(
            "N/A" if r["point2_confidence_score"] is None else f"{r['point2_confidence_score']:.3f}")
        self.P2CorrespondingRating.setText(
            "N/A" if r["point2_confidence_rating"] is None else r["point2_confidence_rating"]
        )
        self.P2Disparity.setText("N/A" if r["disparity"] is None else f"{r['disparity'][0]:.3f}")
        self.P23DPosition.setText(
            "N/A" if r["point2_3d"] is None else f"{','.join(['{a:.3f}' for a in r['point2_3d']])}")

        self.DistanceResult.setText(f"{r['distance_3d']:.3f}")

    def draw_corresponding_points(self):
        for image, label, point_source in zip(
                [self.stereo_pipeline.left_img, self.stereo_pipeline.right_img],
                [self.LeftStereoImage, self.RightStereoImage],
                [self.stereo_pipeline.selected_points, self.stereo_pipeline.corresponding_points]):

            image = QImage(image.tobytes(), image.shape[1], image.shape[0], QImage.Format.Format_BGR888)
            for i, (x, y), color in zip(range(2), point_source, self.stereo_pipeline.point_colors):

                painter = QPainter(image)
                painter.setPen(QColor(*color))
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.drawEllipse(x-5, y-5, 10, 10)
                painter.drawText(x+10, y+5, f"{i+1}")
                painter.end()

            label.setPixmap(QPixmap.fromImage(image))

    def load_photosphere(self):
        file, fltr = QFileDialog.getOpenFileName(
            self,
            "Select File for Photosphere Data",
            str(self.chosen_photosphere_directory),  # Start in current directory
            filter="Images (*.jpg *.jpeg *.JPG *.JPEG)"
        )

        self.photosphere_viewer.set_image_path(file)

    def change_photosphere_directory(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Directory for Photosphere Data",
            str(self.chosen_photosphere_directory),  # Start in current directory
            QFileDialog.Option.ShowDirsOnly
        )

        if directory:  # If user didn't cancel
            print("Selected photosphere directory:", directory)
            self.chosen_photosphere_directory = Path(directory)
        self.PhotospherePictureDirectory.setText(str(self.chosen_photosphere_directory))

    def take_picture(self):
        frame_index = self.PhotosphereCameraSelect.get_selected_item()

        if frame_index is None:
            print("Couldn't take picture - frame index was None", file=sys.stderr)
            return

        with self.data.camera_frames[frame_index].lock:
            frame = self.data.camera_frames[frame_index].frame
            if frame is None:
                print("Couldn't take picture - camera is disconnected. Try a different camera...", file=sys.stderr)
                return
            timestamp = datetime.now().isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
            if not frame.save(str(self.chosen_photosphere_directory / f"{timestamp}.jpg")):
                print("Failed to save picture")
                return
        with open(str(self.chosen_photosphere_directory / f"{timestamp}.rov_data"), "wb") as f:
            pickle.dump(self.data.export_rov_data(), f)
        print("Picture taken!")

    @staticmethod
    def warp_to_equirectangular(img, f, yaw, pitch, roll, canvas_width, canvas_height):
        """
        Warps a single image onto an equirectangular (spherical) canvas
        using the provided IMU data (yaw, pitch, roll).

        Parameters:
          img          : Input image (numpy array)
          f            : Focal length in pixels.
          yaw, pitch, roll : IMU angles in radians.
          canvas_width : Width of the output panorama.
          canvas_height: Height of the output panorama.

        Returns:
          warped       : The warped image on the equirectangular canvas.
        """
        h, w = img.shape[:2]
        cx, cy = w / 2.0, h / 2.0

        # Create a meshgrid for the source image.
        X, Y = np.meshgrid(np.arange(w), np.arange(h))
        Xn = X - cx
        Yn = Y - cy
        Z = np.full_like(Xn, f, dtype=np.float32)
        # Build the 3D direction vectors for each pixel.
        d = np.stack((Xn, Yn, Z), axis=-1).astype(np.float32)

        # Build rotation matrices for yaw, pitch, and roll.
        cyaw, syaw = math.cos(yaw), math.sin(yaw)
        cpitch, spitch = math.cos(pitch), math.sin(pitch)
        croll, sroll = math.cos(roll), math.sin(roll)

        # Yaw rotation about Y axis.
        R_yaw = np.array([[cyaw, 0, syaw],
                          [0, 1, 0],
                          [-syaw, 0, cyaw]], dtype=np.float32)
        # Pitch rotation about X axis.
        R_pitch = np.array([[1, 0, 0],
                            [0, cpitch, -spitch],
                            [0, spitch, cpitch]], dtype=np.float32)
        # Roll rotation about Z axis.
        R_roll = np.array([[croll, -sroll, 0],
                           [sroll, croll, 0],
                           [0, 0, 1]], dtype=np.float32)
        # Combined rotation matrix.
        R = R_yaw @ R_pitch @ R_roll

        # Rotate each direction vector.
        d_reshaped = d.reshape(-1, 3).T  # Shape: (3, N)
        d_rot = R @ d_reshaped
        d_rot = d_rot.T.reshape(h, w, 3)

        # Normalize the rotated vectors.
        norm = np.linalg.norm(d_rot, axis=2, keepdims=True)
        d_norm = d_rot / norm

        # Convert normalized vectors to spherical coordinates.
        # Longitude: arctan2(x, z); Latitude: arcsin(y)
        longitude = np.arctan2(d_norm[..., 0], d_norm[..., 2])
        latitude = np.arcsin(d_norm[..., 1])

        # Map spherical coordinates to canvas coordinates.
        x_canvas = ((longitude + math.pi) / (2 * math.pi)) * canvas_width
        y_canvas = ((math.pi / 2 - latitude) / math.pi) * canvas_height

        # Initialize the output warped image and a weight map for blending.
        warped = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)
        weight = np.zeros((canvas_height, canvas_width, 3), dtype=np.float32)

        # Round canvas coordinates to integer indices.
        x_canvas_int = np.round(x_canvas).astype(np.int32)
        y_canvas_int = np.round(y_canvas).astype(np.int32)

        # Only consider valid indices.
        valid = (x_canvas_int >= 0) & (x_canvas_int < canvas_width) & \
                (y_canvas_int >= 0) & (y_canvas_int < canvas_height)

        # splat each pixel's color into the panorama.
        for channel in range(3):
            np.add.at(warped[..., channel],
                      (y_canvas_int[valid], x_canvas_int[valid]),
                      img[..., channel][valid])
            np.add.at(weight[..., channel],
                      (y_canvas_int[valid], x_canvas_int[valid]),
                      1.0)

        # normalize where the image was splatted.
        weight[weight == 0] = 1.0
        warped /= weight
        warped = np.clip(warped, 0, 255).astype(np.uint8)
        return warped

    @staticmethod
    def composite_images(warped_images):
        """
        Combines a list of warped images into one final panorama using per-pixel averaging.
        """
        composite = np.zeros_like(warped_images[0], dtype=np.float32)
        weight = np.zeros_like(warped_images[0], dtype=np.float32)

        for img in warped_images:
            mask = (img.sum(axis=2) > 0)[:, :, None].astype(np.float32)
            composite += img.astype(np.float32) * mask
            weight += mask

        weight[weight == 0] = 1.0
        composite /= weight
        composite = np.clip(composite, 0, 255).astype(np.uint8)
        return composite

    def attach_data_interface(self) -> None:
        super().attach_data_interface()
        self.PhotosphereCameraSelectContainer: QFrame = self.findChild(QFrame)

        for i in range(len(self.data.camera_frames)):
            self.PhotosphereCameraSelect.add_item(f"Camera {i + 1}", i)

        if len(self.data.camera_frames) > 1:
            self.PhotosphereCameraSelect.set_selected_item("Camera 1")

        if len(self.data.camera_frames) > 0:
            self.PhotosphereCameraSelect.item_selection_changed.connect(
                lambda: self.PhotosphereCameraSelect.set_selected_item("Camera 1")
                if self.PhotosphereCameraSelect.get_selected_item() is None else None)

    def create_photosphere(self, file_dir, canvas_height, canvas_width):
        image_paths = os.listdir(self.chosen_photosphere_directory)
        images = []
        rov_data: [ROVData] = []
        image_names = []

        progress = 0
        for path in image_paths:
            self.photosphere_creation_progress_update.emit(progress)
            progress += 30 // len(image_paths)
            path = self.chosen_photosphere_directory / Path(path)
            if path.is_dir() or not path.name.endswith(".jpg"):
                continue

            timestamp = ".".join(path.name.split(".")[:-1])

            img = cv2.imread(str(path))
            if img is None:
                print(f"Failed to read {path}", file=sys.stderr)
                continue

            try:
                with open(path.parent / f"{timestamp}.rov_data", "rb") as f:
                    data = pickle.load(f)
                rov_data.append(data)
            except (FileNotFoundError, OSError):
                print(f"Could not access {path.parent / f'{timestamp}.rov_data'}", file=sys.stderr)
                continue

            image_names.append(timestamp)
            images.append(img)

        # warp each image based on its IMU data
        warped_images = []
        for i, img in enumerate(images):
            self.photosphere_creation_progress_update.emit(progress)
            progress += 45 // len(images)
            pitch = math.radians(rov_data[i].attitude.x)
            yaw = math.radians(rov_data[i].attitude.y)
            roll = math.radians(rov_data[i].attitude.z)

            print("~~~~~~~~~")
            print(image_names[i])
            print(f"{pitch=}")
            print(f"{yaw=}")
            print(f"{roll=}")

            image_height, image_width = img.shape[:2]

            focal_length = image_width / (2 * math.pi)


            warped = self.warp_to_equirectangular(
                img,
                focal_length,
                yaw,
                pitch,
                roll,
                canvas_width,
                canvas_height
            )
            warped_images.append(warped)

        panorama = self.composite_images(warped_images)

        self.photosphere_creation_progress_update.emit(95)

        cv2.imwrite(str(file_dir), panorama)
        print(f"Panorama saved as {file_dir.name}")

        self.photosphere_creation_progress_update.emit(100)

        return str(file_dir)

    def on_create_photosphere_clicked(self):
        filename, ok = QInputDialog.getText(self, "Photosphere Filename", "Enter a filename for the photosphere: ")
        if not ok:
            return

        file_dir = self.chosen_photosphere_directory / f"{filename}.jpg"

        if file_dir.exists():
            response = QMessageBox.warning(None,
                                           "File Exists",
                                           f"A photosphere called {filename} already exists. "
                                           f"Would you like to overwrite it?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                           QMessageBox.StandardButton.No
                                           )
            if response == QMessageBox.StandardButton.No:
                return

        task = GraphingTask(
            lambda: self.create_photosphere(file_dir, 1024, 2048),
            lambda file_dir: self.photosphere_creation_complete.emit(file_dir) if type(file_dir) is str else None
        )

        QThreadPool.globalInstance().start(task)

    def attach_nav_bar(self, dock) -> None:
        tab_bar = self.tab_widget.tabBar()
        width = 0
        for i in range(tab_bar.count()):
            width += tab_bar.tabRect(i).width()
        self.nav = NavBar(self, dock, nav_bar_right_offset=width)
        self.nav.generate_layout()

    def on_recording_directory_changed(self):
        self.InputRecording.clear_all_items()
        for file in os.listdir(self.recordings_path):
            self.InputRecording.add_item(file)

    def record_fields(self):
        new_row = [time.time()]
        for pretty_field in self.fields_to_record:
            field = self.recordable_fields[pretty_field]
            attrs = field.split(".")
            attr = self.data
            if len(attrs) > 1:
                for i in range(len(attrs) - 1):
                    attr = getattr(attr, attrs[i])
            data = getattr(attr, attrs[-1])
            new_row.append(data)
        try:
            with open(self.recording_file_path, "a") as f:
                text = ",".join([str(f) for f in new_row]) + "\n"
                f.write(text)
        except Exception as e:
            self.toggle_recording()
            QMessageBox.warning(None, "Error", f"Recording Failed Unexpectedly:\n{e}")

        self.recording_dataframe.loc[len(self.recording_dataframe)] = new_row

        self.recording_update.emit()

    def display_field_axes_options(self):
        x_axis_choice = self.XAxis.get_selected_display_text()
        y_axis_choice = self.YAxis.get_selected_display_text()
        z_axis_choice = self.ZAxis.get_selected_display_text()

        self.XAxis.clear_all_items()
        self.YAxis.clear_all_items()
        self.ZAxis.clear_all_items()

        if self.LiveGraph.isChecked() and self.is_recording:
            fields = ["Time"] + self.fields_to_record
        else:
            selected = self.InputRecording.get_selected_item()
            if selected is None:
                return

            selected_file = self.recordings_path / selected
            if selected_file == "":
                return
            try:
                df = pd.read_csv(selected_file)
                fields = list(df.columns)
            except Exception as e:
                print("Couldn't read CSV file:", e, file=sys.stderr)
                return

        for field in fields:
            self.XAxis.add_item(field)

        if x_axis_choice in fields:
            self.XAxis.set_selected_item(x_axis_choice)

        for field in fields:
            self.YAxis.add_item(field)

        if y_axis_choice in fields:
            self.YAxis.set_selected_item(y_axis_choice)

        for field in fields:
            self.ZAxis.add_item(field)

        if z_axis_choice in fields:
            self.ZAxis.set_selected_item(z_axis_choice)

    def toggle_recording(self):
        if not self.is_recording:
            selected_fields = self.RecordedFields.get_selected_display_texts()
            if len(selected_fields) == 0:
                QMessageBox.warning(self, "No Field Selected", f"Please select at least one field to record")
                return

            filename, ok = QInputDialog.getText(None, "Recording Filename", "Enter a filename for the recording: ")
            if not ok:
                return

            new_recording = self.recordings_path / f"{filename}.csv"
            if new_recording.exists():
                response = QMessageBox.warning(None,
                                               "File Exists",
                                               f"A recording called {filename} already exists. "
                                               f"Would you like to overwrite it?",
                                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                               QMessageBox.StandardButton.No
                                               )
                if response == QMessageBox.StandardButton.No:
                    return

            self.recording_file_path = new_recording
            self.recording_dataframe = pd.DataFrame([], columns=["Time"] + selected_fields)
            self.fields_to_record = selected_fields

            try:
                with open(self.recording_file_path, "w+") as f:
                    f.write(",".join(self.recording_dataframe.columns) + "\n")
            except Exception as e:
                QMessageBox.warning(None, "Error", f"Failed to start recording:\n{e}")
                return

            self.StartRecordingButton.setText("Stop Recording")
            self.RecordedFields.set_checkboxes_enabled(False)
            self.recording_timer.start(100)
            self.is_recording = True
            self.LiveGraph.setEnabled(True)
            self.LiveGraph.setChecked(False)

            self.display_field_axes_options()
        else:
            self.StartRecordingButton.setText("Start Recording")
            self.RecordedFields.set_checkboxes_enabled(True)
            self.recording_timer.stop()
            self.is_recording = False
            self.LiveGraph.setEnabled(False)
            self.LiveGraph.setChecked(False)
            self.recording_end.emit()

    def create_graph(self):
        axes = [self.XAxis.get_selected_display_text(), self.YAxis.get_selected_display_text()]
        if self.GraphType.get_selected_display_text() == "3D Graph":
            axes.append(self.ZAxis.get_selected_display_text())
        if any([axis == "" for axis in axes]):
            QMessageBox.warning(None, "Empty Axis", "Please set fields for each axis for the graph")
            return

        title, ok = QInputDialog.getText(None, "Figure Title", "Enter a title for the figure: ")
        if not ok:
            return

        if self.LiveGraph.isChecked():
            graph_widget = GraphWidget(self.recording_dataframe, axes,
                                       self.GraphType.get_selected_display_text() == "3D Graph",
                                       True, title,
                                       recording_update_signal=self.recording_update,
                                       recording_end_signal=self.recording_end)
        else:
            df_file = self.InputRecording.get_selected_item()
            if df_file == "":
                QMessageBox.warning(None, "No Chosen Recording", "Please select a recording to graph")
                return

            df = pd.read_csv(self.recordings_path / df_file)
            graph_widget = GraphWidget(df, axes, self.GraphType.get_selected_display_text() == "3D Graph", False, title)

        if not graph_widget.build_success:
            return

        if self.displaying_no_graph_tab:
            self.displaying_no_graph_tab = False
            self.GraphTabWidget.removeTab(0)

        self.GraphTabWidget.addTab(graph_widget, title)
        self.GraphTabWidget.setCurrentWidget(graph_widget)

    def graph_closed(self):
        if self.GraphTabWidget.count() == 0:
            self.displaying_no_graph_tab = True
            no_graphs_label = QLabel("No Graphs to Display...")
            no_graphs_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.GraphTabWidget.addTab(no_graphs_label, "No Graphs to Display")

    def model_migration(self, start_year: int, end_year: int, migration_years: list[int], input_images: list[str],
                        year_display_positions: list[tuple[int, int]]):
        self.migration_media_player.stop()
        if self.generating_model:
            return
        self.generating_model = True
        assert len(migration_years) == 5 and len(input_images) == 6 and len(year_display_positions) == 6

        # Remove Frames folder and contents

        if os.path.exists("Frames"):
            shutil.rmtree("Frames")
        if not os.path.exists("Frames"):
            os.mkdir("Frames")
        else:
            raise OSError("Failed to remove Frames folder")
        if not os.path.exists("Frames"):
            raise OSError("Failed to recreate Frames folder")

        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 1.5
        color = (0, 0, 255)
        thickness = 5

        # Generate Frames

        current_migration = 0
        for i, year in enumerate(range(start_year, end_year + 1)):
            # Progress carp migration until end
            while current_migration < 5 and migration_years[current_migration] is not None and year >= migration_years[
                current_migration]:
                current_migration += 1

            frame = cv2.imread(input_images[current_migration])
            if frame is None:
                raise ValueError(f"Could not read file: {input_images[current_migration]}")

            cv2.putText(frame, str(year), year_display_positions[current_migration], font, font_scale, color, thickness,
                        cv2.LINE_AA)

            cv2.imwrite(f"Frames\\{i:04}.jpg", frame)

        # Create Video
        print("Creating video", file=sys.__stdout__, flush=True)
        os.system(f"ffmpeg -y -framerate 1 -i Frames\\%4d.jpg migration.mp4 -start_number 0 -vf format=yuv420")

        self.generating_model = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.migration_video_item.setSize(QSizeF(self.VideoContainer.width(), self.VideoContainer.height()))
        self.VideoContainer.scene().setSceneRect(self.migration_video_item.boundingRect())
        self.VideoContainer.fitInView(self.migration_video_item, Qt.AspectRatioMode.KeepAspectRatio)

    def on_generate_model_clicked(self):
        start_year = 2016
        end_year = 2025

        sanitised_years = []
        # Ensure inputted values are either valid years or left blank
        for v in [self.area1, self.area2, self.area3, self.area4, self.area5]:
            v = v.text()
            if v == "":
                sanitised_years.append(None)
                continue
            if not v.isdigit():
                return
            v = int(v)
            if v < start_year or v > end_year:
                return
            sanitised_years.append(v)

        task = GraphingTask(lambda: self.model_migration(start_year, end_year,
                                                         sanitised_years,
                                                         [
                                                             r"Migration Images\Area0.png",
                                                             r"Migration Images\Area1.png",
                                                             r"Migration Images\Area2.png",
                                                             r"Migration Images\Area3.png",
                                                             r"Migration Images\Area4.png",
                                                             r"Migration Images\Area5.png",
                                                         ],
                                                         [
                                                             (70, 830),
                                                             (240, 620),
                                                             (340, 450),
                                                             (440, 380),
                                                             (650, 280),
                                                             (750, 180),
                                                         ]),
                            self.play_migration_video)

        QThreadPool.globalInstance().start(task)

    def play_migration_video(self, callback_in=None):
        video = QUrl.fromLocalFile(f"{os.getcwd()}\\migration.mp4")

        self.migration_media_player.stop()
        self.migration_media_player.setSource(QUrl())

        self.migration_media_player.setSource(video)
        self.migration_media_player.setLoops(QMediaPlayer.Loops.Infinite)

        self.migration_video_item.setSize(QSizeF(self.VideoContainer.width(), self.VideoContainer.height()))
        self.VideoContainer.scene().setSceneRect(self.migration_video_item.boundingRect())
        self.VideoContainer.fitInView(self.migration_video_item, Qt.AspectRatioMode.KeepAspectRatio)

        self.migration_media_player.play()

    def on_eDNA_clicked(self):
        if self.unknown_sample_folder is None or self.eDNA_database is None:
            QMessageBox.warning(self, "Error", f"Please set Unknown Sample and Species Database Folders")
            return
        try:
            def on_task_complete(results):
                for row, (sample_no, score, classification) in enumerate(results):
                    self.eDNAResults.setItem(row, 0, QTableWidgetItem(str(sample_no)))
                    self.eDNAResults.setItem(row, 1, QTableWidgetItem(str(int(score))))
                    self.eDNAResults.setItem(row, 2, QTableWidgetItem(classification))

            task = GraphingTask(
                lambda: self.eDNA_sampler.generate_results(self.unknown_sample_folder, self.eDNA_database),
                on_task_complete)
            QThreadPool.globalInstance().start(task)

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Error in eDNA sampling: {str(e)}")

    def select_eDNA_path(self, dest_attribute: str, dest_label: str, caption: str):
        folder_path = QFileDialog.getExistingDirectory(self, caption)

        if folder_path:
            setattr(self, dest_attribute, folder_path)
            getattr(self, dest_label).setText(str(folder_path))

    def update_button_icon(self, button_name, image_path):
        button = getattr(self, button_name, None)
        if button:
            button.setIcon(QIcon(image_path))
            button.setIconSize(button.size())

    def add_toolbar(self, canvas):
        if self.toolbar is not None:
            self.graphArea.layout().removeWidget(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

        self.toolbar = NavigationToolbar(canvas, self)
        self.graphArea.layout().addWidget(self.toolbar)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Grapher()
    window.show()
    sys.exit(app.exec())
