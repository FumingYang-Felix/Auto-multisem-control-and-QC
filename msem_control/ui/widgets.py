from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel, 
    QGroupBox, QProgressBar, QFrame, QScrollArea, QSizePolicy,
    QToolButton, QFileDialog, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsItem,
    QPushButton, QGraphicsPolygonItem, QGraphicsEllipseItem,
    QGraphicsPathItem
)
from PySide6.QtCore import Qt, Signal, QSize, QPointF, QRectF
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QWheelEvent, QMouseEvent, QIcon, QPolygonF, QBrush, QPainterPath
from datetime import datetime
import json


class ROIPolygonItem(QGraphicsPolygonItem):
    """Custom polygon item that moves its associated support points when moved"""
    
    def __init__(self, polygon, region_idx, parent_map):
        super().__init__(polygon)
        self._region_idx = region_idx
        self._parent_map = parent_map
        self._last_pos = self.pos()
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges)
    
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            # Calculate offset from last position
            new_pos = value
            offset_x = new_pos.x() - self._last_pos.x()
            offset_y = new_pos.y() - self._last_pos.y()
            
            # Move associated support points
            if self._parent_map and self._region_idx in self._parent_map.support_point_items:
                for sp_item in self._parent_map.support_point_items[self._region_idx]:
                    sp_pos = sp_item.pos()
                    sp_item.setPos(sp_pos.x() + offset_x, sp_pos.y() + offset_y)
            
            self._last_pos = new_pos
        
        return super().itemChange(change, value)


class ThumbtackItem(QGraphicsPathItem):
    """Custom graphics item representing a thumbtack (图钉) shape"""
    def __init__(self, x, y, size=20):
        super().__init__()
        
        # Create thumbtack shape: circle head + pin body
        path = QPainterPath()
        
        # Head (circle)
        head_radius = size * 0.6
        path.addEllipse(x - head_radius, y - head_radius, head_radius * 2, head_radius * 2)
        
        # Pin body (tapered rectangle/triangle pointing down)
        pin_width = size * 0.2
        pin_length = size * 0.8
        pin_polygon = QPolygonF([
            QPointF(x - pin_width, y + head_radius * 0.5),  # Left top
            QPointF(x + pin_width, y + head_radius * 0.5),  # Right top
            QPointF(x + pin_width * 0.3, y + head_radius * 0.5 + pin_length),  # Right bottom (tapered)
            QPointF(x - pin_width * 0.3, y + head_radius * 0.5 + pin_length),  # Left bottom (tapered)
        ])
        path.addPolygon(pin_polygon)
        
        self.setPath(path)
        
        # Style
        self.setBrush(QColor(255, 100, 100, 220))  # Red thumbtack
        self.setPen(QPen(QColor(255, 255, 255), 2))
        self.setZValue(100)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable)
        
        # Store center position
        self._center_x = x
        self._center_y = y

class NavigationButton(QPushButton):
    """Button for the left sidebar navigation"""
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFixedHeight(50)
        self.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                color: #aaaaaa;
                text-align: left;
                padding-left: 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #2d2d2d;
                color: white;
            }
            QPushButton:checked {
                background-color: #3e3e3e;
                color: #569cd6; /* Blue */
                border-left: 3px solid #569cd6;
            }
        """)

class Sidebar(QWidget):
    """Left navigation sidebar"""
    page_changed = Signal(int) # Emits index of page
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)
        self.setStyleSheet("background-color: #1e1e1e; border-right: 1px solid #333;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 20, 0, 0)
        layout.setSpacing(5)
        
        # Title
        title = QLabel("MultiSEM")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: white; padding-left: 20px; margin-bottom: 20px;")
        layout.addWidget(title)
        
        # Buttons
        self.btn_control = NavigationButton("Control & Map")
        self.btn_acq = NavigationButton("Acquisition")
        self.btn_settings = NavigationButton("Parameters")
        
        self.btn_group = [self.btn_control, self.btn_acq, self.btn_settings]
        for i, btn in enumerate(self.btn_group):
            layout.addWidget(btn)
            btn.clicked.connect(lambda checked, idx=i: self.on_button_clicked(idx))
            
        layout.addStretch()
        
        # Select first by default
        self.btn_control.setChecked(True)

    def on_button_clicked(self, index):
        for i, btn in enumerate(self.btn_group):
            btn.setChecked(i == index)
        self.page_changed.emit(index)

class CollapsibleBox(QWidget):
    """A collapsible group box for organizing parameters"""
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self._is_collapsed = True
        
        self.toggle_button = QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setStyleSheet("""
            QToolButton { 
                border: none; 
                font-weight: bold; 
                text-align: left; 
                background-color: #2d2d2d; 
                color: #cccccc; 
                padding: 8px;
                width: 100%;
            } 
            QToolButton:hover { 
                background-color: #3e3e3e; 
            }
        """)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.RightArrow)
        self.toggle_button.clicked.connect(self.toggle)
        self.toggle_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.content_area = QWidget()
        self.content_area.setVisible(False)
        
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(0)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self.toggle_button)
        self._layout.addWidget(self.content_area)
        
    def toggle(self):
        self._is_collapsed = not self._is_collapsed
        
        if self._is_collapsed:
            self.toggle_button.setArrowType(Qt.RightArrow)
            self.content_area.setVisible(False)
        else:
            self.toggle_button.setArrowType(Qt.DownArrow)
            self.content_area.setVisible(True)

    def set_content_layout(self, layout):
        self.content_area.setLayout(layout)

class InteractiveMap(QGraphicsView):
    """
    Optical Map View
    - Loads Optical Image (PNG/JPG)
    - Click to Move Stage
    - ROI Drawing
    - Crosshair marker
    """
    stage_moved = Signal(float, float) # Signal(x, y)
    polygon_clicked = Signal(int) # Signal(region_index) when user clicks a polygon
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(30, 30, 30))
        
        self.pixmap_item = None
        self.roi_items = []
        self.crosshair_items = []  # Store crosshair lines
        self.support_point_items = {}  # {region_idx: [QGraphicsEllipseItem, ...]}
        self.dragging_support_point = None  # Currently dragged support point
        
        # Calibration: pixel to um conversion
        self.pixel_size_um = 1.0  # um per pixel
        self.image_offset_x = 0.0  # image origin in um
        self.image_offset_y = 0.0
        
    def load_image(self, path):
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return False
            
        # Clear old items but keep ROIs
        if self.pixmap_item:
            self.scene.removeItem(self.pixmap_item)
        self.clear_crosshair()
        
        self.pixmap_item = QGraphicsPixmapItem(pixmap)
        self.scene.addItem(self.pixmap_item)
        self.pixmap_item.setZValue(-1)  # Behind ROIs
        
        self.setSceneRect(self.pixmap_item.boundingRect())
        self.fitInView(self.pixmap_item, Qt.KeepAspectRatio)
        return True
    
    def set_calibration(self, pixel_size_um: float, offset_x: float = 0, offset_y: float = 0):
        """Set the pixel to um calibration"""
        self.pixel_size_um = pixel_size_um
        self.image_offset_x = offset_x
        self.image_offset_y = offset_y
        
    def pixel_to_um(self, px: float, py: float):
        """Convert pixel coordinates to um (stage coordinates)
        
        X-axis is flipped: stage X = -image X
        """
        ux = -px * self.pixel_size_um + self.image_offset_x
        uy = py * self.pixel_size_um + self.image_offset_y
        return ux, uy
    
    def um_to_pixel(self, ux: float, uy: float):
        """Convert um (stage coordinates) to pixel coordinates
        
        X-axis is flipped: image X = -stage X
        """
        px = -(ux - self.image_offset_x) / self.pixel_size_um
        py = (uy - self.image_offset_y) / self.pixel_size_um
        return px, py
        
    def add_roi(self, x, y, w, h):
        """Add a rectangular ROI (in pixels)"""
        rect = QGraphicsRectItem(x, y, w, h)
        rect.setPen(QPen(QColor(0, 255, 0), 2))
        rect.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable)
        self.scene.addItem(rect)
        self.roi_items.append(rect)

    def add_polygon_roi(self, points: list, color: QColor = None, region_idx: int = None):
        """Add a polygon ROI from list of (x,y) tuples in PIXEL coordinates
        
        Args:
            points: List of (x, y) tuples in pixel coordinates
            color: Optional color for the polygon
            region_idx: Optional region index for tracking (used for support point binding)
        """
        if color is None:
            color = QColor(255, 100, 0)  # Orange
        
        # Use current number of roi_items as region_idx if not provided
        if region_idx is None:
            region_idx = len(self.roi_items)
            
        poly_points = [QPointF(x, y) for x, y in points]
        polygon = ROIPolygonItem(QPolygonF(poly_points), region_idx, self)
        
        # Style: Semi-transparent fill, solid outline
        fill_color = QColor(color)
        fill_color.setAlpha(50)
        polygon.setBrush(fill_color)
        polygon.setPen(QPen(color, 2))
        
        self.scene.addItem(polygon)
        self.roi_items.append(polygon)
        return polygon
    
    def clear_rois(self):
        """Remove all ROI items"""
        for item in self.roi_items:
            self.scene.removeItem(item)
        self.roi_items.clear()
    
    def set_masks_editable(self, editable: bool):
        """Enable or disable editing (moving/rotating) of ROI masks
        
        Args:
            editable: If True, ROIs can be moved and rotated
        """
        base_flags = QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges
        if editable:
            flags = base_flags | QGraphicsItem.ItemIsMovable
        else:
            flags = base_flags
        
        for item in self.roi_items:
            item.setFlags(flags)
        
        # Support points should NOT be independently movable when masks are editable
        # They will move with their parent polygon automatically
        sp_flags = QGraphicsItem.ItemIsSelectable
        if not editable:
            # When locked, support points can still be individually movable
            sp_flags |= QGraphicsItem.ItemIsMovable
        
        for region_items in self.support_point_items.values():
            for sp_item in region_items:
                sp_item.setFlags(sp_flags)
        
        self._masks_editable = editable
        
    def draw_crosshair(self, px: float, py: float, size: float = 500):
        """Draw a crosshair at the given pixel position (scales with zoom to maintain relative size)"""
        self.clear_crosshair()
        
        color = QColor(255, 50, 50, 220)  # Bright red with transparency
        pen = QPen(color, 15)  # Thick line in scene coordinates
        
        # Horizontal line
        h_line = self.scene.addLine(px - size, py, px + size, py, pen)
        h_line.setZValue(200)  # Very high z-value to be on top
        
        # Vertical line
        v_line = self.scene.addLine(px, py - size, px, py + size, pen)
        v_line.setZValue(200)
        
        # Circle (larger for visibility)
        circle_pen = QPen(color, 12)
        circle_size = size * 1.2
        circle = self.scene.addEllipse(px - circle_size, py - circle_size, circle_size*2, circle_size*2, circle_pen)
        circle.setZValue(200)
        
        self.crosshair_items = [h_line, v_line, circle]
        print(f"[InteractiveMap] Drew crosshair at ({px:.1f}, {py:.1f}) with size {size}px (scales with zoom)")
        
    def draw_crosshair_um(self, um_x: float, um_y: float):
        """Draw crosshair at um coordinates (for real-time stage position tracking)"""
        px, py = self.um_to_pixel(um_x, um_y)
        self.draw_crosshair(px, py)
    
    def clear_crosshair(self):
        """Remove crosshair"""
        for item in self.crosshair_items:
            self.scene.removeItem(item)
        self.crosshair_items.clear()
    
    def draw_support_points(self, region_idx: int, points: list, coordinates='pixel'):
        """Draw support points for a region
        
        Args:
            region_idx: Index of the region
            points: List of (x, y) tuples
            coordinates: 'pixel' or 'um' - coordinate system of input points
        """
        # Clear existing support points for this region
        self.clear_support_points(region_idx)
        
        items = []
        for idx, (x, y) in enumerate(points):
            # Convert to pixel coordinates if needed
            if coordinates == 'um':
                px, py = self.um_to_pixel(x, y)
                x_um, y_um = x, y
            else:  # pixel coordinates
                px, py = x, y
                x_um, y_um = self.pixel_to_um(x, y)
            
            # Create a draggable thumbtack with size scaled for visibility
            size = 20  # Base size
            thumbtack = ThumbtackItem(px, py, size)
            thumbtack.setData(0, "support_point")  # Mark as support point
            thumbtack.setData(1, region_idx)  # Store region index
            thumbtack.setData(2, (x_um, y_um))  # Store um coordinates
            thumbtack.setData(3, idx)  # Store point index for table display
            
            self.scene.addItem(thumbtack)
            items.append(thumbtack)
        
        self.support_point_items[region_idx] = items
        print(f"[InteractiveMap] Drew {len(items)} support points (thumbtacks) for region {region_idx} at z-level 100")
    
    def clear_support_points(self, region_idx: int = None):
        """Clear support points for a region (or all if region_idx is None)"""
        if region_idx is None:
            # Clear all support points
            for idx in list(self.support_point_items.keys()):
                for item in self.support_point_items[idx]:
                    self.scene.removeItem(item)
            self.support_point_items.clear()
        else:
            # Clear support points for specific region
            if region_idx in self.support_point_items:
                for item in self.support_point_items[region_idx]:
                    self.scene.removeItem(item)
                del self.support_point_items[region_idx]
    
    def get_support_points(self, region_idx: int) -> list:
        """Get current positions of support points for a region (in um)"""
        if region_idx not in self.support_point_items:
            return []
        
        points = []
        for item in self.support_point_items[region_idx]:
            rect = item.rect()
            center_px = rect.center().x()
            center_py = rect.center().y()
            x_um, y_um = self.pixel_to_um(center_px, center_py)
            points.append((x_um, y_um))
        
        return points
    
    def update_support_point_color(self, region_idx: int, point_idx: int, color: QColor):
        """Update the color of a specific support point"""
        if region_idx in self.support_point_items:
            if 0 <= point_idx < len(self.support_point_items[region_idx]):
                item = self.support_point_items[region_idx][point_idx]
                item.setBrush(color)
                item.setPen(QPen(QColor(255, 255, 255), 2))
    
    def update_realtime_crosshair(self, x_um: float, y_um: float):
        """Update crosshair position to follow stage movement in real-time
        
        Args:
            x_um, y_um: Stage position in um coordinates
        """
        px, py = self.um_to_pixel(x_um, y_um)
        self.draw_crosshair(px, py, size=40)
    
    def draw_tile_at_position(self, x_um: float, y_um: float, size_um: float, 
                              color: QColor = None, tile_id: str = None):
        """Draw a single MFOV tile at position with given color
        
        Args:
            x_um, y_um: Center position in um
            size_um: Tile size (diameter) in um
            color: QColor for tile (red for pending, green for completed)
            tile_id: Identifier for this tile
        
        Returns:
            QGraphicsEllipseItem: The created tile item
        """
        if color is None:
            color = QColor(255, 0, 0, 100)  # Red, semi-transparent
        
        # Convert to pixels
        px, py = self.um_to_pixel(x_um, y_um)
        radius_px = size_um / (2 * self.pixel_size_um)
        
        # Create hexagon (approximate with circle for now)
        tile = QGraphicsEllipseItem(px - radius_px, py - radius_px, 
                                     radius_px * 2, radius_px * 2)
        tile.setBrush(color)
        tile.setPen(QPen(QColor(255, 255, 255, 150), 1))
        tile.setZValue(5)  # Above ROIs, below support points
        tile.setData(0, "mfov_tile")
        if tile_id:
            tile.setData(1, tile_id)
        
        self.scene.addItem(tile)
        return tile
    
    def update_tile_color(self, tile_item, color: QColor):
        """Update the color of an MFOV tile"""
        tile_item.setBrush(color)

    def wheelEvent(self, event: QWheelEvent):
        """Zoom with mouse wheel"""
        zoom_in = event.angleDelta().y() > 0
        factor = 1.25 if zoom_in else 0.8
        self.scale(factor, factor)
    
    def mousePressEvent(self, event: QMouseEvent):
        """Single click to select polygon (if not in drawing mode)"""
        # Always call parent to maintain default behavior (dragging, etc)
        super().mousePressEvent(event)
        
        # In drawing mode, don't check for polygon selection
        # Check for both naming conventions (_drawing_mode and drawing_mode)
        drawing_mode = getattr(self, '_drawing_mode', getattr(self, 'drawing_mode', False))
        if drawing_mode:
            return
        
        # Check if clicked inside a polygon (only for single left-click)
        if event.button() == Qt.LeftButton and not event.modifiers():
            scene_pos = self.mapToScene(event.pos())
            click_point = QPointF(scene_pos.x(), scene_pos.y())
            
            # Handle both naming conventions (_roi_polygons and roi_polygons)
            roi_polygons = getattr(self, '_roi_polygons', getattr(self, 'roi_polygons', []))
            for region_idx, roi_data in enumerate(roi_polygons):
                # Handle both dict format and direct QGraphicsPolygonItem
                if isinstance(roi_data, dict):
                    polygon = roi_data['polygon']
                else:
                    # Direct QGraphicsPolygonItem
                    polygon = roi_data.polygon() if hasattr(roi_data, 'polygon') else roi_data
                
                # Check if click is inside the polygon
                if hasattr(polygon, 'containsPoint'):
                    if polygon.containsPoint(click_point, Qt.OddEvenFill):
                        self.polygon_clicked.emit(region_idx)
                        print(f"[InteractiveMap] Clicked on Region {region_idx + 1}")
                        break
                elif hasattr(roi_data, 'contains'):
                    # QGraphicsPolygonItem has contains() method
                    if roi_data.contains(click_point):
                        self.polygon_clicked.emit(region_idx)
                        print(f"[InteractiveMap] Clicked on Region {region_idx + 1}")
                        break
        
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Double click to move stage to that location"""
        scene_pos = self.mapToScene(event.pos())
        px, py = scene_pos.x(), scene_pos.y()
        
        # Draw crosshair at click position
        self.draw_crosshair(px, py)
        
        # Convert to stage coordinates
        stage_x, stage_y = self.pixel_to_um(px, py)
        
        self.stage_moved.emit(stage_x, stage_y)
        super().mouseDoubleClickEvent(event)

class LogPanel(QGroupBox):
    def __init__(self):
        super().__init__("System Logs")
        self.layout = QVBoxLayout()
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        # Cap the log so a long acquisition (the pipeline subprocess prints many
        # lines per tile) can't grow the document unbounded — an unbounded
        # QTextEdit is a real source of UI lag over a long run.
        self.text_edit.document().setMaximumBlockCount(5000)
        self.layout.addWidget(self.text_edit)
        self.setLayout(self.layout)

    def append_log(self, source: str, level: str, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = "#cccccc"
        if level == "WARNING": color = "#cca700"
        if level == "ERROR": color = "#f44747"
        
        html = f'<span style="color:#569cd6">[{timestamp}]</span> <span style="color:#ce9178">[{source}]</span> <span style="color:{color}">{message}</span>'
        self.text_edit.append(html)

class StatusIndicator(QWidget):
    def __init__(self, label: str):
        super().__init__()
        layout = QHBoxLayout()
        layout.setContentsMargins(0,0,0,0)
        
        self.name_label = QLabel(f"{label}:")
        self.value_label = QLabel("N/A")
        self.value_label.setObjectName("StatusValue")
        
        layout.addWidget(self.name_label)
        layout.addWidget(self.value_label)
        layout.addStretch()
        self.setLayout(layout)

    def set_value(self, value: str, is_error: bool = False):
        self.value_label.setText(str(value))
        if is_error:
            self.value_label.setObjectName("ErrorValue")
        else:
            self.value_label.setObjectName("StatusValue")
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)

class QCPanel(QGroupBox):
    def __init__(self):
        super().__init__("Real-time QC Monitor")
        layout = QVBoxLayout()
        
        self.focus_bar = QProgressBar()
        self.focus_bar.setRange(0, 100)
        self.focus_bar.setFormat("Focus Score: %v")
        
        self.contrast_bar = QProgressBar()
        self.contrast_bar.setRange(0, 100)
        self.contrast_bar.setFormat("Contrast: %v")
        
        layout.addWidget(QLabel("Focus Quality:"))
        layout.addWidget(self.focus_bar)
        layout.addWidget(QLabel("Contrast:"))
        layout.addWidget(self.contrast_bar)
        
        self.setLayout(layout)
        
    def update_qc(self, focus: float, contrast: float):
        self.focus_bar.setValue(int(focus * 10))
        self.contrast_bar.setValue(int(contrast * 100))
        
        if focus < 5.0:
            self.focus_bar.setStyleSheet("QProgressBar::chunk { background-color: #f44747; }")
        else:
            self.focus_bar.setStyleSheet("QProgressBar::chunk { background-color: #4ec9b0; }")


# === Drop Zone Widget ===

class ImageScaleDialog(QWidget):
    """Dialog to choose image scaling option when dropping"""
    
    def __init__(self, filename: str, parent=None):
        from PySide6.QtWidgets import QDialog, QRadioButton, QButtonGroup, QDoubleSpinBox, QDialogButtonBox
        super().__init__(parent)
        
        # This is actually a dialog, create one
        pass


class DropZone(QFrame):
    """
    Drop zone for receiving dragged images.
    Shows a dashed border when empty, and the image preview when loaded.
    """
    import os
    
    image_loaded = Signal(str, bool, float)  # filepath, use_fullscale, scale_percent
    image_cleared = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.current_image = None
        self.current_path = None
        self.use_fullscale = True
        self.scale_percent = 100.0
        
        self.setup_ui()
        self._update_style(False)
    
    def setup_ui(self):
        from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QProgressBar
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Image view
        self.view = QGraphicsView()
        self.scene = QGraphicsScene()
        self.view.setScene(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setStyleSheet("background-color: #0a0a0a; border: none;")
        
        # Progress bar for loading
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminate mode
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #1e1e1e;
            }
            QProgressBar::chunk {
                background-color: #007acc;
            }
        """)
        self.progress_bar.hide()
        
        # Empty state overlay
        self.empty_label = QLabel("Drop image here")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("""
            QLabel {
                color: #666;
                font-size: 14px;
                background-color: transparent;
            }
        """)
        
        # Loading label
        self.loading_label = QLabel("Loading large image...")
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.loading_label.setStyleSheet("""
            QLabel {
                color: #007acc;
                font-size: 12px;
                background-color: rgba(0, 0, 0, 0.7);
                padding: 10px;
                border-radius: 5px;
            }
        """)
        self.loading_label.hide()
        
        # Clear button
        self.btn_clear = QPushButton("✕")
        self.btn_clear.setFixedSize(24, 24)
        self.btn_clear.setStyleSheet("""
            QPushButton {
                background-color: rgba(100, 100, 100, 0.7);
                color: white;
                border-radius: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(200, 50, 50, 0.9);
            }
        """)
        self.btn_clear.clicked.connect(self.clear_image)
        self.btn_clear.hide()
        
        # Info label at bottom
        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("color: #888; font-size: 10px; padding: 4px;")
        self.info_label.hide()
        
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.view)
        layout.addWidget(self.info_label)
        
        self.empty_label.setParent(self.view)
        self.btn_clear.setParent(self.view)
        self.loading_label.setParent(self.view)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.empty_label.isVisible():
            self.empty_label.setGeometry(0, 0, self.view.width(), self.view.height())
        if self.btn_clear.isVisible():
            self.btn_clear.move(self.view.width() - 30, 6)
        if self.loading_label.isVisible():
            # Center the loading label
            lw, lh = 150, 40
            self.loading_label.setGeometry(
                (self.view.width() - lw) // 2,
                (self.view.height() - lh) // 2,
                lw, lh
            )
    
    def _update_style(self, has_image: bool):
        if has_image:
            self.setStyleSheet("""
                DropZone {
                    border: 2px solid #3e3e3e;
                    border-radius: 8px;
                    background-color: #1a1a1a;
                }
            """)
        else:
            self.setStyleSheet("""
                DropZone {
                    border: 2px dashed #555;
                    border-radius: 8px;
                    background-color: #1a1a1a;
                }
            """)
    
    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-msem-image") or mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
            self.setStyleSheet("""
                DropZone {
                    border: 2px dashed #007acc;
                    border-radius: 8px;
                    background-color: #1a2a3a;
                }
            """)
    
    def dragLeaveEvent(self, event):
        self._update_style(self.current_image is not None)
    
    def dropEvent(self, event):
        from PySide6.QtWidgets import QDialog, QRadioButton, QDoubleSpinBox, QDialogButtonBox, QButtonGroup
        import os
        
        mime = event.mimeData()
        filepath = None
        
        if mime.hasFormat("application/x-msem-image"):
            filepath = bytes(mime.data("application/x-msem-image")).decode()
        elif mime.hasText():
            filepath = mime.text()
        elif mime.hasUrls():
            urls = mime.urls()
            if urls:
                filepath = urls[0].toLocalFile()
        
        if filepath and os.path.exists(filepath):
            # Create scale dialog inline
            dialog = QDialog(self)
            dialog.setWindowTitle("Image Import Options")
            dialog.setModal(True)
            dialog.setMinimumWidth(350)
            
            layout = QVBoxLayout(dialog)
            
            info = QLabel(f"Import: {os.path.basename(filepath)}")
            info.setStyleSheet("font-weight: bold; margin-bottom: 10px;")
            layout.addWidget(info)
            
            radio_fullscale = QRadioButton("Use full-scale image")
            radio_fullscale.setChecked(True)
            
            radio_scaled = QRadioButton("Use scaling (%)")
            
            spin_scale = QDoubleSpinBox()
            spin_scale.setRange(1, 100)
            spin_scale.setValue(50)
            spin_scale.setSuffix(" %")
            spin_scale.setEnabled(False)
            
            radio_scaled.toggled.connect(spin_scale.setEnabled)
            
            button_group = QButtonGroup(dialog)
            button_group.addButton(radio_fullscale, 1)
            button_group.addButton(radio_scaled, 2)
            
            layout.addWidget(radio_fullscale)
            
            scale_layout = QHBoxLayout()
            scale_layout.addWidget(radio_scaled)
            scale_layout.addWidget(spin_scale)
            scale_layout.addStretch()
            layout.addLayout(scale_layout)
            
            layout.addSpacing(20)
            
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            
            dialog.setStyleSheet("""
                QDialog { background-color: #2d2d2d; }
                QLabel { color: #cccccc; }
                QRadioButton { color: #cccccc; }
            """)
            
            if dialog.exec() == QDialog.Accepted:
                use_fullscale = radio_fullscale.isChecked()
                scale_percent = 100.0 if use_fullscale else spin_scale.value()
                self.load_image(filepath, use_fullscale, scale_percent)
        
        self._update_style(self.current_image is not None)
        event.acceptProposedAction()
    
    def load_image(self, filepath: str, use_fullscale: bool = True, scale_percent: float = 100.0):
        import os
        from PySide6.QtWidgets import QApplication, QMessageBox
        
        self.current_path = filepath
        self.use_fullscale = use_fullscale
        self.scale_percent = scale_percent
        
        ext = os.path.splitext(filepath)[1].lower()
        
        # Show loading indicator
        file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        
        self.empty_label.hide()
        self.loading_label.setText(f"Loading...\n{os.path.basename(filepath)}")
        self.loading_label.show()
        self.loading_label.setGeometry(
            (self.view.width() - 150) // 2,
            (self.view.height() - 50) // 2,
            150, 50
        )
        self.progress_bar.show()
        QApplication.processEvents()
        
        pixmap = None
        original_size = None
        
        try:
            if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff']:
                pixmap = QPixmap(filepath)
                if not pixmap.isNull():
                    original_size = (pixmap.width(), pixmap.height())
            elif ext == '.czi':
                # Load CZI - returns full resolution pixmap
                pixmap, original_size = self._load_czi_full(filepath)
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.loading_label.hide()
            self.progress_bar.hide()
        
        if pixmap and not pixmap.isNull():
            # Check if image is too large (> 50000x50000)
            if original_size:
                w, h = original_size
                if w > 50000 or h > 50000:
                    reply = QMessageBox.warning(
                        self,
                        "Large Image Warning",
                        f"This image is very large ({w}x{h}).\n\n"
                        f"Loading full resolution may cause:\n"
                        f"• High memory usage (~{w*h/1024/1024:.0f} MB)\n"
                        f"• Slow performance\n\n"
                        f"Continue with full resolution?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No
                    )
                    if reply == QMessageBox.No:
                        self.empty_label.show()
                        return
            
            self.scene.clear()
            
            # Apply scaling if not full-scale
            if not use_fullscale:
                new_size = pixmap.size() * (scale_percent / 100.0)
                pixmap = pixmap.scaled(
                    new_size.toSize(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            
            self.current_image = QGraphicsPixmapItem(pixmap)
            self.scene.addItem(self.current_image)
            self.view.setSceneRect(self.current_image.boundingRect())
            self.view.fitInView(self.current_image, Qt.KeepAspectRatio)
            
            self.empty_label.hide()
            self.btn_clear.show()
            self.btn_clear.raise_()
            
            # Display info
            display_w, display_h = pixmap.width(), pixmap.height()
            scale_info = "Full scale" if use_fullscale else f"{scale_percent:.0f}%"
            size_str = f"{file_size / 1024 / 1024:.1f} MB" if file_size > 0 else ""
            self.info_label.setText(f"{os.path.basename(filepath)} | {display_w}x{display_h} | {scale_info} | {size_str}")
            self.info_label.show()
            
            self._update_style(True)
            self.image_loaded.emit(filepath, use_fullscale, scale_percent)
            
            self.btn_clear.move(self.view.width() - 30, 6)
        else:
            self.empty_label.show()
            print(f"Failed to load image: {filepath}")
    
    def _load_czi_full(self, filepath: str):
        """
        Load CZI file at FULL resolution (no subsampling).
        Handles both regular and mosaic CZI files.
        
        Returns:
            (QPixmap, (width, height)) - pixmap and original size tuple
        """
        try:
            from aicspylibczi import CziFile
            import numpy as np
            from PySide6.QtGui import QImage
            from PySide6.QtWidgets import QApplication
            
            czi = CziFile(filepath)
            QApplication.processEvents()
            
            # Extract pixel size from metadata
            pixel_size_um = 1.0  # Default
            try:
                import xml.etree.ElementTree as ET
                metadata_xml = czi.meta
                root = ET.fromstring(metadata_xml)
                
                # Find ScalingX (pixel size in um)
                for distance in root.findall(".//Distance[@Id='X']"):
                    value_elem = distance.find('Value')
                    if value_elem is not None:
                        pixel_size_um = float(value_elem.text) * 1e6  # Convert m to um
                        print(f"DEBUG: Pixel size from metadata: {pixel_size_um:.6f} um/pixel")
                        break
            except Exception as e:
                print(f"DEBUG: Could not extract pixel size from metadata: {e}")
            
            # Check if this is a mosaic file
            if czi.is_mosaic():
                # Mosaic CZI - use read_mosaic
                bbox = czi.get_mosaic_bounding_box()
                full_x, full_y = bbox.w, bbox.h
                
                self.loading_label.setText(f"Loading mosaic {full_x}x{full_y}...")
                QApplication.processEvents()
                
                # Get dimensions to determine C (channel) count
                dims = czi.get_dims_shape()[0]
                c_count = dims.get('C', (0, 1))[1] if isinstance(dims.get('C'), tuple) else dims.get('C', 1)
                print(f"DEBUG: CZI has {c_count} channels")
                
                # Read full resolution mosaic - must specify C dimension
                try:
                    # Read without downsampling - use region to get full bbox, specify C=0
                    mosaic = czi.read_mosaic(region=(bbox.x, bbox.y, bbox.w, bbox.h), scale_factor=1.0, C=0)
                    print(f"DEBUG: Read mosaic with explicit region and C=0: {mosaic.shape}")
                except Exception as e1:
                    print(f"DEBUG: First attempt failed: {e1}")
                    try:
                        # Fallback: read without region but with C=0
                        mosaic = czi.read_mosaic(scale_factor=1.0, C=0)
                        print(f"DEBUG: Read mosaic with C=0: {mosaic.shape}")
                    except Exception as e2:
                        print(f"DEBUG: Second attempt failed: {e2}")
                        # Final fallback: try reading all channels
                        mosaic = czi.read_mosaic(scale_factor=1.0)
                        print(f"DEBUG: Read mosaic without C specification: {mosaic.shape}")
                
                QApplication.processEvents()
                self.loading_label.setText("Processing...")
                QApplication.processEvents()
                
                print(f"DEBUG: Mosaic shape before processing: {mosaic.shape}")
                
                # Handle multi-dimensional arrays to extract RGB image
                # Typical shapes: (1, H, W, C) or (H, W, C) or (C, H, W)
                if len(mosaic.shape) == 4:
                    # (1, H, W, C) -> (H, W, C)
                    img = mosaic[0, :, :, :]
                    print(f"DEBUG: After squeeze dim 0: {img.shape}")
                elif len(mosaic.shape) == 3:
                    img = mosaic
                else:
                    img = np.squeeze(mosaic)
                
                # Ensure we have (H, W, C) format
                if len(img.shape) == 3:
                    if img.shape[0] <= 4 and img.shape[0] < img.shape[1] and img.shape[0] < img.shape[2]:
                        # Likely (C, H, W) -> transpose to (H, W, C)
                        img = np.transpose(img, (1, 2, 0))
                        print(f"DEBUG: Transposed from (C,H,W) to (H,W,C): {img.shape}")
                    
                    # If we have more than 3 channels, take first 3 (RGB)
                    if img.shape[2] > 3:
                        img = img[:, :, :3]
                        print(f"DEBUG: Reduced to RGB: {img.shape}")
                    elif img.shape[2] == 1:
                        # Grayscale - convert to RGB by replicating
                        img = np.repeat(img, 3, axis=2)
                        print(f"DEBUG: Converted grayscale to RGB: {img.shape}")
                
                print(f"DEBUG: Final image shape: {img.shape}")
                
            else:
                # Regular CZI - use read_image
                dims = czi.get_dims_shape()[0]
                full_x = dims.get('X', (0, 1000))[1] if isinstance(dims.get('X'), tuple) else dims.get('X', 1000)
                full_y = dims.get('Y', (0, 1000))[1] if isinstance(dims.get('Y'), tuple) else dims.get('Y', 1000)
                
                self.loading_label.setText(f"Loading {full_x}x{full_y}...")
                QApplication.processEvents()
                
                img, _ = czi.read_image()
                
                QApplication.processEvents()
                self.loading_label.setText("Processing...")
                QApplication.processEvents()
                
                img = np.squeeze(img)
                # Convert to (H, W, C) if needed
                if len(img.shape) == 3 and img.shape[0] <= 4:
                    img = np.transpose(img, (1, 2, 0))
            
            # Get original dimensions
            if len(img.shape) == 3:
                orig_h, orig_w, channels = img.shape
            else:
                orig_h, orig_w = img.shape
                channels = 1
            
            # Normalize with percentile clipping for better contrast
            if img.dtype != np.uint8:
                if len(img.shape) == 3:
                    # RGB image - normalize each channel
                    for c in range(img.shape[2]):
                        pmin, pmax = np.percentile(img[:, :, c], (1, 99))
                        img[:, :, c] = np.clip((img[:, :, c] - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255)
                    img = img.astype(np.uint8)
                else:
                    # Grayscale
                    pmin, pmax = np.percentile(img, (1, 99))
                    img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
            
            # Create QImage
            if len(img.shape) == 3 and img.shape[2] == 3:
                # RGB image
                h, w, c = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w * 3, QImage.Format_RGB888)
                print(f"CZI loaded (full, RGB): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
            else:
                # Grayscale image
                if len(img.shape) == 3:
                    img = img[:, :, 0]  # Take first channel
                h, w = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
                print(f"CZI loaded (full, Grayscale): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
            
            # Return pixmap, size, and pixel_size_um
            return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um
            
        except ImportError:
            # Try czifile as fallback
            try:
                import czifile
                import numpy as np
                from PySide6.QtGui import QImage
                from PySide6.QtWidgets import QApplication
                
                self.loading_label.setText("Loading (czifile)...")
                QApplication.processEvents()
                
                # Try to extract pixel size from metadata
                pixel_size_um = 1.0  # Default
                try:
                    import xml.etree.ElementTree as ET
                    with czifile.CziFile(filepath) as czi:
                        metadata_xml = czi.metadata()
                        root = ET.fromstring(metadata_xml)
                        
                        for distance in root.findall(".//Distance[@Id='X']"):
                            value_elem = distance.find('Value')
                            if value_elem is not None:
                                pixel_size_um = float(value_elem.text) * 1e6
                                print(f"DEBUG: Pixel size from czifile metadata: {pixel_size_um:.6f} um/pixel")
                                break
                except Exception as e:
                    print(f"DEBUG: Could not extract pixel size from czifile: {e}")
                
                with czifile.CziFile(filepath) as czi:
                    img = czi.asarray()
                
                img = np.squeeze(img)
                if len(img.shape) > 2:
                    img = img[0] if len(img.shape) == 3 else img[0, 0]
                
                orig_h, orig_w = img.shape
                
                if img.dtype != np.uint8:
                    pmin, pmax = np.percentile(img, (1, 99))
                    img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
                
                h, w = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
                
                print(f"CZI loaded (czifile, full): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
                
                return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um
                
            except Exception as e:
                print(f"CZI load error (czifile): {e}")
                return None, None, 1.0
                
        except Exception as e:
            print(f"CZI load error: {e}")
            import traceback
            traceback.print_exc()
            return None, None, 1.0
    
    def clear_image(self):
        self.scene.clear()
        self.current_image = None
        self.current_path = None
        
        self.empty_label.show()
        self.empty_label.setGeometry(0, 0, self.view.width(), self.view.height())
        self.btn_clear.hide()
        self.info_label.hide()
        
        self._update_style(False)
        self.image_cleared.emit()
    
    def get_image_info(self):
        if self.current_path:
            return {
                'path': self.current_path,
                'use_fullscale': self.use_fullscale,
                'scale_percent': self.scale_percent
            }
        return None
