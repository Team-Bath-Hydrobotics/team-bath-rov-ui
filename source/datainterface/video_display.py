import sys

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QFont, QColor
from PyQt6.QtWidgets import QLabel

from datainterface.video_frame import VideoFrame

import typing

if typing.TYPE_CHECKING:
    from app import App

# This class is used to generate a pixmap for a given UI label.
# The UI can connect to the pixmap_ready signal to update the pixmap displayed on a label.

pitch_yaw_overlay_font = QFont("Helvetica", 12)
depth_font = QFont("Helvetica", 10)
pixel_depth_spacing = 100
overlay_colour = QColor(255, 255, 255)


class VideoDisplay(QObject):
    pixmap_ready = pyqtSignal(QPixmap)
    on_disconnect = pyqtSignal()

    def __init__(self, label: QLabel, frame_index, app: "App" = None, overlay=False, vertical_aov=90):
        self.label = label
        self.frame_index = frame_index
        self.camera_feed: VideoFrame | None = None
        self.app = app
        self.overlay = overlay
        self.attitude_center_pixmap = QPixmap("datainterface/attitudeCenter.png")
        self.attitude_lines_pixmap = QPixmap("datainterface/attitudeLines.png")
        self.depth_pixmap = QPixmap("datainterface/depthIndicator.png")
        self.vertical_aov = vertical_aov

        super().__init__()

    def attach_camera_feed(self) -> None:
        camera_frame_count = len(self.app.data_interface.camera_frames)
        if self.frame_index >= camera_frame_count:
            print(f"Frame Index {self.frame_index} out of range {0} - {camera_frame_count-1}. "
                  f"Increase the camera feed count to avoid this", file=sys.stderr)
            return
        camera_feed = self.app.data_interface.camera_frames[self.frame_index]
        # Remove any old connections to the new frame signal if reattaching camera feed
        if self.camera_feed:
            self.camera_feed.new_frame.disconnect(self.update_frame)
        self.camera_feed = camera_feed
        # Create a new connection to the current camera feed when a new frame is available.
        self.camera_feed.new_frame.connect(self.update_frame)
        self.on_disconnect.emit()

    def update_frame(self) -> None:
        if self.camera_feed is None:
            raise AttributeError("A Camera Feed Is Not Attached")
        # Wait until VideoFrame is free
        with self.camera_feed.lock:
            frame = self.camera_feed.frame
            if frame is not None:
                # Generate the pixmap that will put onto a label
                rect = self.label.geometry()
                pixmap = QPixmap(frame.copy())
                # Ensure image fits available space as best as possible.

                if rect.width() > rect.height():
                    pixmap = pixmap.scaledToWidth(rect.width())
                else:
                    pixmap = pixmap.scaledToHeight(rect.height())

                if self.overlay:
                    w, h = pixmap.width(), pixmap.height()

                    center_h, center_w = self.attitude_center_pixmap.height(), self.attitude_center_pixmap.width()
                    line_height, line_width = self.attitude_lines_pixmap.height(), self.attitude_lines_pixmap.width()
                    depth_h, depth_w = self.depth_pixmap.height(), self.depth_pixmap.width()

                    painter = QPainter(pixmap)
                    painter.drawPixmap((w - center_w) // 2, (h - center_h) // 2, center_w, center_h,
                                       self.attitude_center_pixmap)

                    painter.save()

                    # roll rotation is attitude.z
                    # pitch rotation is attitude.x

                    # Calculate the vertical height of the attitude indicator to be aligned with the horizon
                    pitch = self.app.data_interface.attitude.x
                    if 90 >= pitch >= -90:
                        v_height = h * pitch / self.vertical_aov
                    elif pitch > 90:
                        v_height = h * (pitch - 180) / self.vertical_aov
                    elif pitch < -90:
                        v_height = h * (180 + pitch) / self.vertical_aov

                    painter.translate(w // 2, h // 2 + v_height)
                    painter.rotate(self.app.data_interface.attitude.z)

                    painter.drawPixmap(-line_width // 2, -line_height // 2, line_width, line_height,
                                       self.attitude_lines_pixmap)

                    painter.restore()

                    painter.save()

                    painter.setPen(overlay_colour)
                    painter.setFont(pitch_yaw_overlay_font)
                    painter.drawText(w // 2 - 100, h // 2 + 50, f"{self.app.data_interface.attitude.x:.1f}°")
                    painter.drawText(w // 2 + 100, h // 2 + 50, f"{self.app.data_interface.attitude.y:.1f}°")

                    painter.restore()

                    painter.save()

                    depth = self.app.data_interface.depth

                    painter.setPen(overlay_colour)
                    painter.setFont(depth_font)
                    painter.drawText(w - 80, h - 100, f"{depth:.2f} m")
                    painter.drawPixmap(w - 100, h - 175, depth_w, depth_h, self.depth_pixmap)

                    for d in range(0, 20, 1):
                        d /= 2
                        px = int((d - depth) * pixel_depth_spacing) + depth_h // 2
                        if 0 <= px <= depth_h - 10:
                            painter.fillRect(w - 110, px + h - 172, 10, 5, overlay_colour)
                            painter.drawText(w - 130, px + h - 165, f"{d:.1f}")
                    painter.restore()

                    painter.end()

                # Emit signal so that a connected label can update their pixmap.
                self.pixmap_ready.emit(pixmap)
            else:
                # Video feed has disconnected if frame is None
                self.on_disconnect.emit()
