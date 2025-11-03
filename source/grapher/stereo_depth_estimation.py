import pathlib

import cv2
import numpy as np

from PyQt6.QtCore import QObject, pyqtSignal


class StereoVisionPipeline(QObject):
    corresponding_point_found = pyqtSignal()
    new_disparity_map = pyqtSignal()
    new_result = pyqtSignal()

    def __init__(self):
        self.disparity_map_display = None
        self.left_img = None
        self.right_img = None
        self.left_img_rect = None
        self.right_img_rect = None
        self.selected_points = []
        self.corresponding_points = []
        self.disparity_map = None
        self.Q = None  # Reprojection matrix
        self.baseline = 0.1  # Default baseline in mm (adjust based on your setup)
        self.focal_length = 700  # Default focal length in pixels (adjust based on your camera)

        # For visualization
        self.point_colors = [(0, 0, 255), (0, 255, 0)]  # Red, Green for points 1 and 2

        self.results = {
            "point1_confidence_score": None,
            "point1_confidence_rating": None,

            "point2_confidence_score": None,
            "point2_confidence_rating": None,

            'distance_3d': None,
            'point1_3d': None,
            'point2_3d': None,
            'depths': None,
        }

        super().__init__()

    def load_images(self, left_path, right_path):
        """Load stereo image pair"""
        self.left_img = cv2.imread(left_path)
        self.right_img = cv2.imread(right_path)

        if self.left_img is None or self.right_img is None:
            raise ValueError("Could not load one or both images")

        # Convert to grayscale for processing
        self.left_gray = cv2.cvtColor(self.left_img, cv2.COLOR_BGR2GRAY)
        self.right_gray = cv2.cvtColor(self.right_img, cv2.COLOR_BGR2GRAY)

        self.compute_disparity()

        print(f"Loaded images: Left {self.left_img.shape}, Right {self.right_img.shape}")

    def simple_rectification(self):
        """
        Simple rectification assuming images are already roughly aligned
        In a real scenario, you'd use camera calibration data
        """
        # For demonstration, we'll assume images are already rectified
        # In practice, you'd use cv2.stereoRectify() with calibration parameters
        self.left_img_rect = self.left_img.copy()
        self.right_img_rect = self.right_img.copy()

        # Create a simple reprojection matrix Q
        # This is a simplified version - in practice, get this from stereo calibration
        h, w = self.left_gray.shape
        self.Q = np.float32([
            [1, 0, 0, -w / 2],
            [0, 1, 0, -h / 2],
            [0, 0, 0, self.focal_length],
            [0, 0, -1 / self.baseline, 0]
        ])

    def compute_disparity(self):
        """Compute disparity map using Semi-Global Block Matching"""
        # Parameters for SGBM
        min_disp = 0
        num_disp = 64  # Must be divisible by 16
        block_size = 11

        # Create SGBM object
        stereo = cv2.StereoSGBM.create(
            minDisparity=min_disp,
            numDisparities=num_disp,
            blockSize=block_size,
            P1=8 * 3 * block_size ** 2,
            P2=32 * 3 * block_size ** 2,
            disp12MaxDiff=1,
            uniquenessRatio=15,
            speckleWindowSize=0,
            speckleRange=2,
            preFilterCap=63,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY
        )

        # Compute disparity
        disparity = stereo.compute(self.left_gray, self.right_gray).astype(np.float32) / 16.0
        self.disparity_map = disparity
        self.disparity_map_display = np.clip(disparity * 255 / np.max(disparity), 0, 255).astype(np.int8)

        cv2.imwrite("../example_disparity_map.jpg", self.disparity_map)

        print("Disparity map computed")

        self.new_disparity_map.emit()

        return disparity

    def find_corresponding_point(self, point_left):
        is_point1 = len(self.selected_points) == 1
        """
        Find corresponding point in right image using template matching
        """
        x, y = point_left

        # Define template size
        template_size = 21
        search_range = 100

        # Extract template from left image
        half_size = template_size // 2

        # Ensure template bounds are within image
        y1 = max(0, y - half_size)
        y2 = min(self.left_gray.shape[0], y + half_size + 1)
        x1 = max(0, x - half_size)
        x2 = min(self.left_gray.shape[1], x + half_size + 1)

        template = self.left_gray[y1:y2, x1:x2]

        if template.size == 0:
            return None

        # Search area in right image (along epipolar line - same y coordinate)
        search_x1 = max(0, x - search_range)
        search_x2 = min(self.right_gray.shape[1], x + search_range)
        search_y1 = max(0, y - half_size)
        search_y2 = min(self.right_gray.shape[0], y + half_size + 1)

        search_area = self.right_gray[search_y1:search_y2, search_x1:search_x2]

        if search_area.size == 0:
            return None

        # Template matching
        result = cv2.matchTemplate(search_area, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        # Convert back to image coordinates
        match_x = search_x1 + max_loc[0] + template.shape[1] // 2
        match_y = search_y1 + max_loc[1] + template.shape[0] // 2

        if is_point1:
            self.results["point1_confidence_score"] = max_val
        else:
            self.results["point2_confidence_score"] = max_val

        # Quality check
        if max_val > 0.7:  # Threshold for good match
            rating = "good"
        else:
            rating = "poor"
            print(f"Low confidence match (confidence: {max_val:.3f})")

        if is_point1:
            self.results["point1_confidence_rating"] = rating
            self.new_result.emit()
        else:
            self.results["point2_confidence_rating"] = rating

        return (match_x, match_y)

    def calculate_3d_distance(self, point1_left, point2_left, point1_right, point2_right):
        """
        Calculate real-world 3D distance between two points
        """
        # Calculate disparities
        disp1 = point1_left[0] - point1_right[0]
        disp2 = point2_left[0] - point2_right[0]

        # Avoid division by zero
        if disp1 <= 0 or disp2 <= 0:
            print("Invalid disparity values")
            return None

        # Calculate depths using simple stereo geometry
        # Z = (focal_length * baseline) / disparity
        depth1 = (self.focal_length * self.baseline) / disp1
        depth2 = (self.focal_length * self.baseline) / disp2

        # Convert pixel coordinates to world coordinates
        # X = (x - cx) * Z / focal_length
        # Y = (y - cy) * Z / focal_length
        cx = self.left_img.shape[1] / 2  # Principal point x
        cy = self.left_img.shape[0] / 2  # Principal point y

        # Point 1 in 3D
        x1_3d = (point1_left[0] - cx) * depth1 / self.focal_length
        y1_3d = (point1_left[1] - cy) * depth1 / self.focal_length
        z1_3d = depth1

        # Point 2 in 3D
        x2_3d = (point2_left[0] - cx) * depth2 / self.focal_length
        y2_3d = (point2_left[1] - cy) * depth2 / self.focal_length
        z2_3d = depth2

        # Calculate 3D Euclidean distance
        distance_3d = np.sqrt((x2_3d - x1_3d) ** 2 + (y2_3d - y1_3d) ** 2 + (z2_3d - z1_3d) ** 2)

        return {
            'distance_3d': distance_3d,
            'point1_3d': (x1_3d, y1_3d, z1_3d),
            'point2_3d': (x2_3d, y2_3d, z2_3d),
            'depths': (depth1, depth2),
            'disparities': (disp1, disp2)
        }

    def add_point(self, x, y):
        if len(self.selected_points) < 2:
            print(f"\nPoint {len(self.selected_points) + 1} selected at: ({x}, {y})")

            # Add selected point
            self.selected_points.append((x, y))

            # Find corresponding point
            corresponding = self.find_corresponding_point((x, y))
            if corresponding:
                self.corresponding_points.append(corresponding)
                print(f"Corresponding point found at: {corresponding}")

                # Calculate distance if we have 2 points
                if len(self.selected_points) == 2:
                    result = self.calculate_3d_distance(
                        self.selected_points[0], self.selected_points[1],
                        self.corresponding_points[0], self.corresponding_points[1]
                    )

                    if result:
                        for key in result:
                            self.results[key] = result[key]

                        self.new_result.emit()

                self.corresponding_point_found.emit()
            else:
                print("Failed to find corresponding point")
                # Remove the point we just added since we couldn't find correspondence
                self.selected_points.pop()

    def reset_points(self):
        """Reset all selected points"""
        self.selected_points.clear()
        self.corresponding_points.clear()
        print("\nPoints reset. Click to select new points.")
