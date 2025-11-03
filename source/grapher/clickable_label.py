from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLabel


class ClickableLabel(QLabel):
    """Custom QLabel that emits click coordinates"""

    # Custom signal that emits the click coordinates
    clicked = pyqtSignal(int, int)  # x, y coordinates

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)  # Optional: track mouse movement
        self.original_pixmap = None
        self.scale_factor_x = 1.0
        self.scale_factor_y = 1.0

    def setPixmap(self, pixmap):
        """Override setPixmap to store original and calculate scaling"""
        self.original_pixmap = pixmap
        super().setPixmap(pixmap)
        self.calculate_scale_factors()

    def calculate_scale_factors(self):
        """Calculate scaling factors between displayed and original image"""
        if self.original_pixmap and not self.original_pixmap.isNull():
            # Get the displayed size (accounting for scaling mode)
            displayed_size = self.size()
            original_size = self.original_pixmap.size()

            if self.hasScaledContents():
                # If scaled contents, use widget size
                self.scale_factor_x = original_size.width() / displayed_size.width()
                self.scale_factor_y = original_size.height() / displayed_size.height()
            else:
                # If not scaled, factor is 1:1
                self.scale_factor_x = 1.0
                self.scale_factor_y = 1.0

    def mousePressEvent(self, event: QMouseEvent):
        """Handle mouse press events"""
        if event.button() == Qt.MouseButton.LeftButton:
            # Get click position relative to the widget
            click_pos = event.position().toPoint()

            # Convert to original image coordinates
            original_x = int(click_pos.x() * self.scale_factor_x)
            original_y = int(click_pos.y() * self.scale_factor_y)

            # Ensure coordinates are within bounds
            if self.original_pixmap and not self.original_pixmap.isNull():
                max_x = self.original_pixmap.width() - 1
                max_y = self.original_pixmap.height() - 1

                original_x = max(0, min(original_x, max_x))
                original_y = max(0, min(original_y, max_y))

            # Emit the signal with coordinates
            self.clicked.emit(original_x, original_y)

        # Call parent implementation
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        """Handle resize events to recalculate scaling"""
        super().resizeEvent(event)
        self.calculate_scale_factors()