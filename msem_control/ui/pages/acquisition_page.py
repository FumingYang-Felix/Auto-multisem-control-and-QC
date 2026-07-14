"""
Acquisition Page

Redesigned layout:
- Left: Settings panel with Setup button and parameters
- Center: Large interactive map for ROI/Tile visualization (drag images here)
- The map supports click-to-move like Control page
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QComboBox, QDoubleSpinBox, QGroupBox, QSplitter, QScrollArea,
    QFormLayout, QLineEdit, QSpinBox, QProgressBar, QTabWidget,
    QFrame, QGridLayout, QCheckBox, QFileDialog, QMessageBox,
    QGraphicsView, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QApplication
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from .base_page import BasePage
from ..widgets import CollapsibleBox, InteractiveMap, QCPanel, LogPanel, DropZone


class TileMapView(InteractiveMap):
    """
    Extended InteractiveMap that accepts dropped images.
    Same interaction as Control page map - click to move stage.
    Supports drawing ROI polygons with mouse.
    """
    
    image_dropped = Signal(str)  # filepath
    roi_completed = Signal(list)  # list of (x, y) points
    tile_count_changed = Signal(int)  # number of tiles
    
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setAcceptDrops(True)
        self._has_image = False

        # Drawing state
        self._drawing_mode = False
        self._current_polygon_points = []
        self._current_point_items = []  # Track point circles for cleanup
        self._current_line_items = []   # Track line segments for cleanup
        self._temp_line = None
        self._close_indicator = None
        self._hover_timer = None
        self._near_start_point = False
        
        # ROI polygons (completed)
        self._roi_polygons = []  # List of QGraphicsPolygonItem
        self._highlighted_roi_idx = -1  # Currently highlighted ROI index
        self._original_polygon_styles = []  # Store original styles for unhighlighting
        
        # Support points for autofocus (per region)
        self._support_points = {}  # {region_idx: [(x, y), ...]}
        self._support_point_items = {}  # {region_idx: [QGraphicsEllipseItem, ...]}
        self._dragging_support_point = None  # Currently dragged point
        
        # Visualization options
        self._show_overlap = False
        self._show_mfov = False
        self._mfov_grid_items = []  # Store MFOV grid graphics items
        
        # Real-time position crosshair
        self._position_crosshair = None
        
        # Manual offset adjustment (for aligning image with ROIs)
        self._manual_offset_x_um = 0.0
        self._manual_offset_y_um = 0.0
        
        # Placeholder text
        self.placeholder = QLabel("Drop image here or use Setup button")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("""
            QLabel {
                color: #666;
                font-size: 16px;
                background-color: transparent;
            }
        """)
        self.placeholder.setParent(self.viewport())
        
        # Hover timer for close indicator
        from PySide6.QtCore import QTimer
        self._hover_timer = QTimer()
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._show_close_indicator)
        
        # Zoom limits
        self._zoom_level = 1.0
        self._min_zoom = 0.01  # Allow very small zoom for large images
        self._max_zoom = 50.0
        
        # Enable mouse tracking for smooth zooming
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        
        # Image dimensions for coordinate conversion
        self._image_width = 0
        self._image_height = 0
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.placeholder.isVisible():
            self.placeholder.setGeometry(0, 0, self.viewport().width(), self.viewport().height())
    
    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-msem-image") or mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
            self.setStyleSheet("background-color: #1a2a3a; border: 2px dashed #007acc;")
    
    def dragLeaveEvent(self, event):
        self.setStyleSheet("background-color: #1e1e1e;")
    
    def dropEvent(self, event):
        self.setStyleSheet("background-color: #1e1e1e;")
        
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
        
        if filepath:
            import os
            if os.path.exists(filepath):
                self.image_dropped.emit(filepath)
        
        event.acceptProposedAction()
    
    def load_image_with_progress(self, filepath: str):
        """Load image and show in map"""
        import os
        from PySide6.QtWidgets import QApplication, QProgressDialog
        from PySide6.QtCore import Qt
        
        # Show progress dialog
        progress = QProgressDialog("Loading image...", None, 0, 0, self)
        progress.setWindowTitle("Loading")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        
        try:
            ext = os.path.splitext(filepath)[1].lower()
            
            pixmap = None
            pixel_size_um = 1.0  # Default
            orig_w, orig_h = 0, 0
            scene_center_x_um, scene_center_y_um = 0.0, 0.0  # Scene center in stage coordinates
            
            if ext == '.czi':
                try:
                    result = self._load_czi_full(filepath)
                    if result and result[0] is not None:
                        if len(result) >= 3:
                            pixmap, (orig_w, orig_h), pixel_size_um = result[:3]
                        else:
                            pixmap, (orig_w, orig_h) = result[:2]
                            pixel_size_um = 1.0
                except Exception as czi_error:
                    print(f"ERROR loading CZI: {czi_error}")
                    import traceback
                    traceback.print_exc()
                    progress.close()
                    from PySide6.QtWidgets import QMessageBox
                    QMessageBox.critical(self, "CZI Load Error", 
                        f"Failed to load CZI file:\n{str(czi_error)}\n\nCheck console for details.")
                    return False
            else:
                from PySide6.QtGui import QPixmap
                pixmap = QPixmap(filepath)
            
            # Use (0, 0) as base - image center is at origin
            self._czi_scene_center_x = 0.0
            self._czi_scene_center_y = 0.0
            
            if pixmap and not pixmap.isNull():
                from PySide6.QtWidgets import QGraphicsPixmapItem
                
                self.scene.clear()
                self.pixmap_item = QGraphicsPixmapItem(pixmap)
                self.scene.addItem(self.pixmap_item)
                self.pixmap_item.setZValue(-1)
                
                # Store dimensions for coordinate conversion
                self._image_width = pixmap.width()
                self._image_height = pixmap.height()
                
                # Position image so that CENTER is at (0, 0) in scene coordinates
                # Top-left corner at (-width/2, -height/2)
                self.pixmap_item.setPos(-self._image_width / 2, -self._image_height / 2)
                
                # Set scene rect centered at (0, 0)
                self.setSceneRect(
                    -self._image_width / 2, -self._image_height / 2,
                    self._image_width, self._image_height
                )
                self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
                
                # Set calibration for coordinate conversion
                # Use scene center position from CZI as the base offset
                # Plus any manual offset for fine-tuning
                base_offset_x = getattr(self, '_czi_scene_center_x', 0.0)
                base_offset_y = getattr(self, '_czi_scene_center_y', 0.0)
                total_offset_x = base_offset_x + self._manual_offset_x_um
                total_offset_y = base_offset_y + self._manual_offset_y_um
                self.set_calibration(pixel_size_um, total_offset_x, total_offset_y)
                print(f"  Calibration offset: base=({base_offset_x:.2f}, {base_offset_y:.2f}), manual=({self._manual_offset_x_um:.2f}, {self._manual_offset_y_um:.2f})")
                
                self.placeholder.hide()
                self._has_image = True
                
                # Calculate image size in um for info
                image_width_um = self._image_width * pixel_size_um
                image_height_um = self._image_height * pixel_size_um
                
                print(f"Loaded: {filepath} ({pixmap.width()}x{pixmap.height()} px)")
                print(f"  Image size: {image_width_um:.2f} x {image_height_um:.2f} um")
                print(f"  Image center at (0, 0), pixel size: {pixel_size_um:.6f} um/px")
                return True
            else:
                QMessageBox.warning(self, "Error", f"Failed to load image: {filepath}")
                return False
                
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Error loading image: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            progress.close()
    
    def set_manual_offset(self, offset_x_um: float, offset_y_um: float):
        """Set manual offset for fine-tuning image-to-ROI alignment
        
        Args:
            offset_x_um: X offset in um (positive = shift ROIs left/image right)
            offset_y_um: Y offset in um (positive = shift ROIs down/image up)
        """
        self._manual_offset_x_um = offset_x_um
        self._manual_offset_y_um = offset_y_um
        # Update calibration with base offset (from CZI) + manual offset
        base_offset_x = getattr(self, '_czi_scene_center_x', 0.0)
        base_offset_y = getattr(self, '_czi_scene_center_y', 0.0)
        total_offset_x = base_offset_x + offset_x_um
        total_offset_y = base_offset_y + offset_y_um
        self.set_calibration(self.pixel_size_um, total_offset_x, total_offset_y)
        print(f"[TileMapView] Offset: base=({base_offset_x:.2f}, {base_offset_y:.2f}), manual=({offset_x_um:.2f}, {offset_y_um:.2f}), total=({total_offset_x:.2f}, {total_offset_y:.2f}) um")
    
    def get_manual_offset(self):
        """Get current manual offset"""
        return self._manual_offset_x_um, self._manual_offset_y_um
    
    def _load_czi(self, filepath: str, progress):
        """Load CZI file (mosaic or regular)"""
        from PySide6.QtGui import QPixmap, QImage
        from PySide6.QtWidgets import QApplication
        import numpy as np
        
        try:
            from aicspylibczi import CziFile
            print("DEBUG: Using aicspylibczi")
            
            czi = CziFile(filepath)
            
            is_mosaic = czi.is_mosaic()
            print(f"DEBUG: Is mosaic: {is_mosaic}")
            
            if is_mosaic:
                progress.setLabelText("Loading mosaic CZI...")
                QApplication.processEvents()
                
                # Read full resolution
                mosaic = czi.read_mosaic(C=0, scale_factor=1.0)
                
                print(f"DEBUG: Mosaic shape before processing: {mosaic.shape}")
                
                # Extract 2D - handle various dimension orders
                if len(mosaic.shape) == 4:
                    # (C, H, W, Z) or similar
                    img = mosaic[0, :, :, 0]
                elif len(mosaic.shape) == 3:
                    # Could be (C, H, W) or (H, W, C)
                    # Take the slice with largest 2D area
                    if mosaic.shape[0] <= 3:  # Likely (C, H, W)
                        img = mosaic[0, :, :]
                    else:  # Likely (H, W, C)
                        img = mosaic[:, :, 0]
                else:
                    img = np.squeeze(mosaic)
                
                print(f"DEBUG: Image shape after extraction: {img.shape}")
            else:
                progress.setLabelText("Loading CZI...")
                QApplication.processEvents()
                
                result = czi.read_image()
                img = result[0] if isinstance(result, tuple) else result
                img = np.squeeze(img)
                if len(img.shape) > 2:
                    img = img[0] if len(img.shape) == 3 else img[0, 0]
            
            progress.setLabelText("Processing...")
            QApplication.processEvents()
            
            # Normalize
            if img.dtype != np.uint8:
                pmin, pmax = np.percentile(img, (1, 99))
                img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
            
            print(f"DEBUG: Final image shape before QImage: {img.shape}")
            
            # Ensure correct shape (H, W)
            if len(img.shape) != 2:
                print(f"ERROR: Expected 2D image, got shape {img.shape}")
                return None
            
            h, w = img.shape
            img = np.ascontiguousarray(img)
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
            pixmap = QPixmap.fromImage(qimg.copy())
            print(f"DEBUG: Created pixmap {pixmap.width()}x{pixmap.height()}")
            return pixmap
            
        except (ImportError, Exception) as e:
            # Fallback to czifile
            print(f"DEBUG: Using czifile fallback ({type(e).__name__}: {e})")
            
            try:
                import czifile
                
                with czifile.CziFile(filepath) as czi:
                    img = czi.asarray()
                
                print(f"DEBUG: czifile raw shape: {img.shape}")
                
                # Remove all dimensions of size 1
                img = np.squeeze(img)
                print(f"DEBUG: After squeeze: {img.shape}")
                
                # Handle multi-dimensional arrays
                while len(img.shape) > 2:
                    # Find the two largest dimensions (should be H and W)
                    sizes = [(i, s) for i, s in enumerate(img.shape)]
                    sizes.sort(key=lambda x: x[1], reverse=True)
                    
                    # If we have (H, W, C) where C is small (channels)
                    if img.shape[-1] <= 4:  # Last dim is likely channels
                        img = img[..., 0]  # Take first channel
                        print(f"DEBUG: Took channel 0, new shape: {img.shape}")
                    # If first dim is small (likely channels)
                    elif img.shape[0] <= 4:
                        img = img[0]  # Take first channel
                        print(f"DEBUG: Took first slice, new shape: {img.shape}")
                    else:
                        # Take the middle slice of the smallest dimension
                        smallest_dim = min(enumerate(img.shape), key=lambda x: x[1])[0]
                        img = np.take(img, img.shape[smallest_dim] // 2, axis=smallest_dim)
                        print(f"DEBUG: Took middle of dim {smallest_dim}, new shape: {img.shape}")
                
                print(f"DEBUG: Final 2D shape: {img.shape}")
                
                if img.dtype != np.uint8:
                    pmin, pmax = np.percentile(img, (1, 99))
                    img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
                
                h, w = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
                pixmap = QPixmap.fromImage(qimg.copy())
                print(f"DEBUG: czifile pixmap {pixmap.width()}x{pixmap.height()}")
                return pixmap
            except Exception as e2:
                print(f"ERROR: czifile also failed: {e2}")
                return None
    
    def clear_map(self):
        """Clear the loaded image"""
        self.scene.clear()
        self.pixmap_item = None
        self._has_image = False
        self._roi_polygons.clear()
        self._current_polygon_points.clear()
        self._current_point_items.clear()
        self._current_line_items.clear()
        self._drawing_mode = False
        self.placeholder.show()
        self.placeholder.setGeometry(0, 0, self.viewport().width(), self.viewport().height())
    
    # === Drawing Mode ===
    
    def set_drawing_mode(self, enabled: bool):
        """Enable/disable polygon drawing mode"""
        self._drawing_mode = enabled
        if enabled:
            # Disable scroll hand drag to allow drawing
            self.setDragMode(QGraphicsView.NoDrag)
            # Set cross cursor on viewport
            self.viewport().setCursor(Qt.CrossCursor)
            self._current_polygon_points.clear()
            self._current_point_items.clear()
            self._current_line_items.clear()
        else:
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.viewport().setCursor(Qt.OpenHandCursor)
            self._clear_temp_graphics()
    
    def _clear_temp_graphics(self):
        """Clear temporary drawing graphics"""
        if self._temp_line:
            self.scene.removeItem(self._temp_line)
            self._temp_line = None
        if self._close_indicator:
            self.scene.removeItem(self._close_indicator)
            self._close_indicator = None
        
        # Clear all point markers
        for item in self._current_point_items:
            if item.scene() == self.scene:
                self.scene.removeItem(item)
        self._current_point_items.clear()
        
        # Clear all line segments
        for item in self._current_line_items:
            if item.scene() == self.scene:
                self.scene.removeItem(item)
        self._current_line_items.clear()
    
    def mousePressEvent(self, event):
        if self._drawing_mode and event.button() == Qt.LeftButton:
            scene_pos = self.mapToScene(event.pos())
            
            # Check if clicking near start point to close polygon
            if len(self._current_polygon_points) >= 3 and self._is_near_start(scene_pos):
                self._complete_polygon()
                return
            
            # Add point
            self._current_polygon_points.append((scene_pos.x(), scene_pos.y()))
            point_item = self._draw_point(scene_pos.x(), scene_pos.y())
            self._current_point_items.append(point_item)
            
            print(f"Added point at ({scene_pos.x():.3f}, {scene_pos.y():.3f}), total: {len(self._current_polygon_points)}")
            
            # Draw line from previous point
            if len(self._current_polygon_points) >= 2:
                line_item = self._draw_segment()
                if line_item:
                    self._current_line_items.append(line_item)
            
            # Force update
            self.viewport().update()
            
            return
        
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        if self._drawing_mode and self._current_polygon_points:
            scene_pos = self.mapToScene(event.pos())
            
            # Update temp line from last point to cursor
            self._update_temp_line(scene_pos)
            
            # Check if near start point
            if len(self._current_polygon_points) >= 3:
                if self._is_near_start(scene_pos):
                    if not self._near_start_point:
                        self._near_start_point = True
                        self._hover_timer.start(200)  # 0.2s delay
                else:
                    self._near_start_point = False
                    self._hover_timer.stop()
                    self._hide_close_indicator()
            
            return
        
        super().mouseMoveEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        """Double click to move stage (when not drawing)"""
        if not self._drawing_mode:
            scene_pos = self.mapToScene(event.pos())
            
            # Scene coordinates are already centered at (0,0)
            x, y = scene_pos.x(), scene_pos.y()
            
            # Draw crosshair at scene position
            self.draw_crosshair(x, y)
            
            # Convert to physical coordinates if calibration is set
            # For now, emit scene coordinates (pixels from center)
            stage_x, stage_y = self.pixel_to_um(x, y)
            self.stage_moved.emit(stage_x, stage_y)
        
        # Don't call super - we handle it
    
    def get_scene_coordinates(self, event_pos) -> tuple:
        """Get scene coordinates from mouse position (center = 0,0)"""
        scene_pos = self.mapToScene(event_pos)
        return (round(scene_pos.x(), 3), round(scene_pos.y(), 3))
    
    def keyPressEvent(self, event):
        """ESC to cancel drawing, Enter to complete"""
        if self._drawing_mode:
            if event.key() == Qt.Key_Escape:
                self._cancel_drawing()
                return
            elif event.key() == Qt.Key_Return and len(self._current_polygon_points) >= 3:
                self._complete_polygon()
                return
        super().keyPressEvent(event)
    
    def wheelEvent(self, event):
        """Zoom with mouse wheel - works both in and out"""
        # Get zoom direction
        delta = event.angleDelta().y()
        
        if delta > 0:
            # Zoom in
            factor = 1.25
        else:
            # Zoom out
            factor = 0.8
        
        # Check zoom limits
        new_zoom = self._zoom_level * factor
        if new_zoom < self._min_zoom or new_zoom > self._max_zoom:
            return
        
        self._zoom_level = new_zoom
        self.scale(factor, factor)
        
        # Debounce MFOV grid update - only update after scrolling stops
        # Use a timer to avoid redrawing on every wheel event
        if self._show_mfov:
            if not hasattr(self, '_mfov_redraw_timer'):
                from PySide6.QtCore import QTimer
                self._mfov_redraw_timer = QTimer(self)
                self._mfov_redraw_timer.setSingleShot(True)
                self._mfov_redraw_timer.timeout.connect(self._delayed_mfov_redraw)
            
            # Reset timer on each wheel event
            self._mfov_redraw_timer.start(300)  # 300ms delay
        
        # Don't call super - we handle it completely
        event.accept()
    
    def _delayed_mfov_redraw(self):
        """Redraw MFOV grid after zoom stabilizes"""
        if self._show_mfov:
            if self._zoom_level > 0.5:
                self._clear_mfov_grid()
                self._draw_mfov_grid()
            else:
                self._clear_mfov_grid()
    
    def _is_near_start(self, pos, threshold=15):
        """Check if position is near the start point"""
        if not self._current_polygon_points:
            return False
        start_x, start_y = self._current_polygon_points[0]
        dx = pos.x() - start_x
        dy = pos.y() - start_y
        return (dx * dx + dy * dy) < threshold * threshold
    
    def _draw_point(self, x, y, color=None):
        """Draw a point marker"""
        from PySide6.QtGui import QPen, QBrush
        if color is None:
            color = QColor(255, 100, 0)  # Orange
        
        # Much larger size for visibility on large images
        size = 200
        ellipse = self.scene.addEllipse(
            x - size/2, y - size/2, size, size,
            QPen(color, 15),
            QBrush(color)
        )
        ellipse.setZValue(100)
        print(f"Drew point at ({x:.1f}, {y:.1f}) with size {size}, z-value 100")
        return ellipse
    
    def _draw_segment(self):
        """Draw line segment between last two points"""
        from PySide6.QtGui import QPen
        if len(self._current_polygon_points) < 2:
            return None
        
        p1 = self._current_polygon_points[-2]
        p2 = self._current_polygon_points[-1]
        
        line = self.scene.addLine(
            p1[0], p1[1], p2[0], p2[1],
            QPen(QColor(255, 100, 0), 20)
        )
        line.setZValue(99)
        print(f"Drew line from ({p1[0]:.1f}, {p1[1]:.1f}) to ({p2[0]:.1f}, {p2[1]:.1f})")
        return line
    
    def _update_temp_line(self, cursor_pos):
        """Update temporary line from last point to cursor"""
        from PySide6.QtGui import QPen
        
        if self._temp_line:
            self.scene.removeItem(self._temp_line)
        
        if self._current_polygon_points:
            last = self._current_polygon_points[-1]
            self._temp_line = self.scene.addLine(
                last[0], last[1], cursor_pos.x(), cursor_pos.y(),
                QPen(QColor(255, 100, 0, 128), 15, Qt.DashLine)
            )
            self._temp_line.setZValue(98)
    
    def _show_close_indicator(self):
        """Show visual indicator that polygon can be closed"""
        from PySide6.QtGui import QPen, QBrush
        
        if not self._current_polygon_points:
            return
        
        start_x, start_y = self._current_polygon_points[0]
        
        # Pulsing circle around start point
        if self._close_indicator:
            self.scene.removeItem(self._close_indicator)
        
        size = 400
        self._close_indicator = self.scene.addEllipse(
            start_x - size/2, start_y - size/2, size, size,
            QPen(QColor(0, 255, 0), 20),
            QBrush(QColor(0, 255, 0, 50))
        )
        self._close_indicator.setZValue(101)
        print(f"Showing close indicator at ({start_x:.1f}, {start_y:.1f})")
    
    def _hide_close_indicator(self):
        """Hide the close indicator"""
        if self._close_indicator:
            self.scene.removeItem(self._close_indicator)
            self._close_indicator = None
    
    def _complete_polygon(self):
        """Complete the current polygon and add as ROI"""
        from PySide6.QtGui import QPen, QBrush, QPolygonF
        from PySide6.QtWidgets import QGraphicsPolygonItem
        from PySide6.QtCore import QPointF
        
        if len(self._current_polygon_points) < 3:
            return
        
        # Create closed polygon
        points = [QPointF(x, y) for x, y in self._current_polygon_points]
        polygon = QGraphicsPolygonItem(QPolygonF(points))
        
        # Style
        color = QColor(255, 100, 0)
        fill = QColor(color)
        fill.setAlpha(50)
        polygon.setBrush(fill)
        polygon.setPen(QPen(color, 20))
        polygon.setZValue(50)
        
        self.scene.addItem(polygon)
        self._roi_polygons.append(polygon)
        
        # Emit signal with points
        self.roi_completed.emit(list(self._current_polygon_points))
        
        # Clear temp graphics
        self._clear_temp_graphics()
        self._current_polygon_points.clear()
        self._near_start_point = False
        
        print(f"ROI completed with {len(points)} points")
    
    def _cancel_drawing(self):
        """Cancel current drawing"""
        self._clear_temp_graphics()
        self._current_polygon_points.clear()
        self._near_start_point = False
    
    def add_roi_from_points(self, points: list, color: QColor = None):
        """Add ROI polygon from list of (x, y) tuples"""
        from PySide6.QtGui import QPen, QBrush, QPolygonF
        from PySide6.QtWidgets import QGraphicsPolygonItem
        from PySide6.QtCore import QPointF
        
        if color is None:
            color = QColor(255, 100, 0)
        
        qpoints = [QPointF(x, y) for x, y in points]
        polygon = QGraphicsPolygonItem(QPolygonF(qpoints))
        
        fill = QColor(color)
        fill.setAlpha(50)
        polygon.setBrush(fill)
        polygon.setPen(QPen(color, 20))
        polygon.setZValue(50)
        
        self.scene.addItem(polygon)
        self._roi_polygons.append(polygon)
        
        return polygon
    
    def clear_rois(self):
        """Clear all ROI polygons"""
        for poly in self._roi_polygons:
            self.scene.removeItem(poly)
        self._roi_polygons.clear()
        self._current_polygon_points.clear()
        self._clear_temp_graphics()
    
    def get_roi_count(self):
        """Get number of ROI polygons"""
        return len(self._roi_polygons)
    
    def zoom_by_factor(self, factor: float):
        """Zoom in or out by a given factor"""
        # Check zoom limits
        new_zoom = self._zoom_level * factor
        if new_zoom < self._min_zoom or new_zoom > self._max_zoom:
            return
        
        self._zoom_level = new_zoom
        self.scale(factor, factor)
        
        # Trigger delayed MFOV redraw (same as wheelEvent)
        if self._show_mfov:
            if not hasattr(self, '_mfov_redraw_timer'):
                from PySide6.QtCore import QTimer
                self._mfov_redraw_timer = QTimer(self)
                self._mfov_redraw_timer.setSingleShot(True)
                self._mfov_redraw_timer.timeout.connect(self._delayed_mfov_redraw)
            self._mfov_redraw_timer.start(100)  # Shorter delay for button clicks
    
    def fit_to_view(self):
        """Fit the entire image to view"""
        if self.pixmap_item:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
            # Reset zoom level tracking
            self._zoom_level = 1.0
            # Clear MFOV grid when zoomed out
            if self._show_mfov:
                self._clear_mfov_grid()
    
    def highlight_roi(self, idx: int):
        """Highlight a specific ROI polygon on the map"""
        from PySide6.QtGui import QPen
        try:
            import shiboken6
        except ImportError:
            shiboken6 = None
        
        def is_valid(obj):
            """Check if Qt object is still valid"""
            if obj is None:
                return False
            if shiboken6:
                return shiboken6.isValid(obj)
            # Fallback: try to access the object
            try:
                obj.isVisible()
                return True
            except RuntimeError:
                return False
        
        # Unhighlight previous
        if self._highlighted_roi_idx >= 0 and self._highlighted_roi_idx < len(self._roi_polygons):
            prev_poly = self._roi_polygons[self._highlighted_roi_idx]
            # Check if object is still valid before accessing
            if is_valid(prev_poly):
                # Restore original style
                prev_poly.setPen(QPen(QColor(255, 100, 0), 20))
        
        # Highlight new
        if idx >= 0 and idx < len(self._roi_polygons):
            poly = self._roi_polygons[idx]
            if is_valid(poly):
                # Make it brighter and thicker
                poly.setPen(QPen(QColor(255, 200, 0), 30))
                poly.setZValue(60)  # Bring to front
                self._highlighted_roi_idx = idx
                
                # Center view on this ROI
                self.centerOn(poly)
    
    def set_visualization_options(self, show_overlap: bool, show_mfov: bool):
        """Update visualization options and redraw"""
        self._show_overlap = show_overlap
        self._show_mfov = show_mfov
        
        # Clear existing MFOV grid
        self._clear_mfov_grid()
        
        # Redraw if needed
        if self._show_mfov and self._zoom_level > 0.5:  # Only show at sufficient zoom
            self._draw_mfov_grid()
    
    def _clear_mfov_grid(self):
        """Clear MFOV grid graphics"""
        for item in self._mfov_grid_items:
            if item.scene() == self.scene:
                self.scene.removeItem(item)
        self._mfov_grid_items.clear()
    
    def _draw_mfov_grid(self):
        """Draw MFOV hexagonal grid on ALL defined ROI regions"""
        from PySide6.QtGui import QPen, QPolygonF, QColor, QBrush
        from PySide6.QtCore import QPointF
        from PySide6.QtWidgets import QGraphicsPolygonItem
        import math
        
        # Get all ROI regions from parent AcquisitionPage
        parent = self.parent()
        while parent and not hasattr(parent, '_roi_list'):
            parent = parent.parent()
        
        if not parent or not hasattr(parent, '_roi_list'):
            # Fallback to local roi_list
            roi_list = getattr(self, '_roi_list', [])
        else:
            roi_list = parent._roi_list
        
        if not roi_list:
            print("[TileMapView] No ROI regions defined")
            return
        
        # MFOV parameters
        mfov_diameter = 110.0  # µm
        hex_radius = mfov_diameter / 2.0
        scale = self.pixel_size_um if self.pixel_size_um > 0 else 1.0
        
        total_tiles = 0
        
        # Draw for each region
        for region_idx, points in enumerate(roi_list):
            if len(points) < 3:
                continue
            
            # Get tiles for this region
            tiles = self._get_tiles_for_region(points)
            if not tiles:
                tiles = self._estimate_tiles_geometric(points)
            
            if not tiles:
                continue
            
            # Color based on whether it's highlighted
            if region_idx == self._highlighted_roi_idx:
                pen_color = QColor(100, 200, 255, 220)  # Brighter blue for highlighted
                fill_color = QColor(100, 200, 255, 50)
                pen_width = 3
            else:
                pen_color = QColor(100, 180, 255, 140)  # Lighter blue for non-highlighted
                fill_color = QColor(100, 180, 255, 20)
                pen_width = 2
            
            # Draw hexagons at each tile position
            # tiles are in µm coordinates
            
            # Debug: print first few tiles to check coordinates and spacing
            # Also compute actual spacing to adjust hex_radius if needed
            actual_spacing = None
            if region_idx == 0 and len(tiles) >= 2:
                t0 = tiles[0]
                t1 = tiles[1]
                if hasattr(t0, 'x'):
                    d = ((t1.x - t0.x)**2 + (t1.y - t0.y)**2)**0.5
                    actual_spacing = d
                    print(f"[DEBUG MFOV] First tile: ({t0.x:.1f}, {t0.y:.1f}) µm")
                    print(f"[DEBUG MFOV] Second tile: ({t1.x:.1f}, {t1.y:.1f}) µm")
                    print(f"[DEBUG MFOV] Tile spacing: {d:.1f} µm (expected ~78-95 µm for 5% overlap)")
                    
                    # If spacing is much larger than expected, MFOVs might be using different size
                    # Estimate actual MFOV diameter from spacing
                    # For flat-top hex: horizontal spacing = 0.75 * diameter (at 0% overlap)
                    # Diagonal spacing = sqrt((0.75*d)^2 + (0.433*d)^2) = 0.866 * diameter
                    estimated_diameter = d / 0.866  # Assume diagonal spacing
                    if abs(estimated_diameter - 110) > 20:
                        print(f"[DEBUG MFOV] WARNING: Estimated MFOV diameter from spacing: {estimated_diameter:.1f} µm (expected 110 µm)")
                        print(f"[DEBUG MFOV] Using actual spacing to size hexagons for correct visualization")
                        # Adjust hex_radius to match actual tile spacing
                        # This ensures hexagons visually match the API-returned positions
                        hex_radius = estimated_diameter / 2.0
                        
                print(f"[DEBUG MFOV] hex_radius={hex_radius:.1f} µm, scale={scale} µm/px, hex_radius_px={hex_radius/scale:.1f} px")
            
            for idx, tile in enumerate(tiles):
                if hasattr(tile, 'x'):
                    tx_um, ty_um = tile.x, tile.y
                else:
                    tx_um, ty_um = tile[0], tile[1]
                
                # Convert tile center from µm to pixels
                px, py = self.um_to_pixel(tx_um, ty_um)
                
                # Convert hex_radius from µm to pixels
                hex_radius_px = hex_radius / scale
                
                # Create hexagon (flat-top orientation, like Zen software)
                hex_points = []
                for i in range(6):
                    angle = math.pi / 3 * i  # Flat-top: start at 0°
                    hx = px + hex_radius_px * math.cos(angle)
                    hy = py + hex_radius_px * math.sin(angle)
                    hex_points.append(QPointF(hx, hy))
                
                hex_poly = QGraphicsPolygonItem(QPolygonF(hex_points))
                hex_poly.setPen(QPen(pen_color, pen_width))
                hex_poly.setBrush(QBrush(fill_color))
                hex_poly.setZValue(45)
                hex_poly.setData(0, f"mfov_{region_idx}_{idx}")
                
                self.scene.addItem(hex_poly)
                self._mfov_grid_items.append(hex_poly)
                total_tiles += 1
        
        if total_tiles > 0:
            print(f"[TileMapView] Drew {total_tiles} MFOV hexagons for {len(roi_list)} region(s)")
    
    def _estimate_tiles_geometric(self, points_px: list) -> list:
        """
        Estimate tile positions using geometric calculation.
        Fallback when API not available.
        
        Args:
            points_px: Polygon vertices in PIXEL coordinates
            
        Returns:
            List of tile positions in µm coordinates
        """
        import math
        
        if len(points_px) < 3:
            return []
        
        # Convert pixel coordinates to µm for calculation
        points_um = [self.pixel_to_um(px, py) for px, py in points_px]
        
        # Get bounding box in µm
        xs = [p[0] for p in points_um]
        ys = [p[1] for p in points_um]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        
        # MFOV parameters
        mfov_diameter = 110.0  # µm (fixed for 91-beam MSEM)
        
        # Get overlap from parent if available
        overlap_pct = 0.04  # 4% default
        parent = self.parent()
        while parent and not hasattr(parent, 'spin_overlap_mfov'):
            parent = parent.parent()
        if parent and hasattr(parent, 'spin_overlap_mfov'):
            overlap_pct = parent.spin_overlap_mfov.value() / 100.0
        
        # Calculate step size for hexagonal packing (in µm)
        # For flat-top hexagons (like Zen software):
        # At 0% overlap, adjacent hexagons just touch:
        #   Horizontal step (same row) = 1.5 * radius = 0.75 * diameter
        #   Vertical step (between rows) = sqrt(3) * radius = 0.866 * diameter
        # With overlap%, reduce step to create overlap
        base_step_x = mfov_diameter * 0.75   # Base horizontal step (edge-to-edge contact)
        base_step_y = mfov_diameter * 0.866  # Base vertical step
        
        # Apply overlap to REDUCE the step (more overlap = smaller step)
        step_x = base_step_x * (1 - overlap_pct)
        step_y = base_step_y * (1 - overlap_pct)
        
        # Generate hex grid in µm
        tiles = []
        row = 0
        y = min_y
        while y <= max_y:
            # Offset every other row by half vertical step for flat-top hex packing
            x_offset = (step_x / 2) if row % 2 == 1 else 0
            x = min_x + x_offset
            
            while x <= max_x:
                # Check if tile center is roughly inside polygon
                # Simple point-in-polygon test
                if self._point_in_polygon(x, y, points_um):
                    tiles.append((x, y))
                x += step_x
            
            y += step_y
            row += 1
        
        return tiles
    
    def _point_in_polygon(self, x: float, y: float, polygon: list) -> bool:
        """Simple ray-casting algorithm to check if point is inside polygon"""
        n = len(polygon)
        inside = False
        
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        
        return inside
    
    def draw_acquisition_mfov_grid(self, region_idx: int, tiles: list):
        """
        Draw MFOV grid for acquisition visualization.
        All tiles start as blue (pending), will be updated to green when acquired.
        
        Args:
            region_idx: Region index
            tiles: List of TilePosition objects from ROI calculation
        """
        from PySide6.QtGui import QPen, QPolygonF, QColor, QBrush
        from PySide6.QtCore import QPointF
        from PySide6.QtWidgets import QGraphicsPolygonItem
        import math
        
        # Clear existing grid for this region
        self._clear_region_mfov_items(region_idx)
        
        # Store items by (region_idx, tile_idx)
        if not hasattr(self, '_acquisition_tile_items'):
            self._acquisition_tile_items = {}
        
        # Estimate MFOV diameter from actual tile spacing (like _draw_mfov_grid)
        mfov_diameter = 110.0  # Default µm
        if len(tiles) >= 2:
            t0, t1 = tiles[0], tiles[1]
            if hasattr(t0, 'x'):
                d = ((t1.x - t0.x)**2 + (t1.y - t0.y)**2)**0.5
                # For flat-top hex: diagonal spacing ≈ 0.866 * diameter
                estimated_diameter = d / 0.866
                if abs(estimated_diameter - 110) > 20:
                    mfov_diameter = estimated_diameter
                    print(f"[Acquisition MFOV] Using estimated diameter: {mfov_diameter:.1f} µm")
        
        hex_radius = mfov_diameter / 2.0
        scale = self.pixel_size_um if self.pixel_size_um > 0 else 1.0
        
        for idx, tile in enumerate(tiles):
            tx, ty = tile.x, tile.y
            px, py = self.um_to_pixel(tx, ty)
            
            # Create hexagon (flat-top, like Zen software)
            hex_points = []
            for i in range(6):
                angle = math.pi / 3 * i  # Flat-top: start at 0°
                hx = px + (hex_radius / scale) * math.cos(angle)
                hy = py + (hex_radius / scale) * math.sin(angle)
                hex_points.append(QPointF(hx, hy))
            
            hex_poly = QGraphicsPolygonItem(QPolygonF(hex_points))
            
            # Pending style: blue outline, transparent fill
            hex_poly.setPen(QPen(QColor(80, 150, 255, 200), 3))
            hex_poly.setBrush(QBrush(QColor(80, 150, 255, 40)))
            hex_poly.setZValue(46)
            
            self.scene.addItem(hex_poly)
            self._acquisition_tile_items[(region_idx, idx)] = hex_poly
        
        print(f"[TileMapView] Drew {len(tiles)} acquisition MFOVs for region {region_idx + 1}")
    
    def update_mfov_acquired(self, region_idx: int, tile_idx: int):
        """
        Update MFOV visualization after acquisition (pending -> acquired).
        
        Args:
            region_idx: Region index
            tile_idx: Tile index
        """
        from PySide6.QtGui import QPen, QBrush, QColor
        
        if not hasattr(self, '_acquisition_tile_items'):
            return
        
        key = (region_idx, tile_idx)
        if key not in self._acquisition_tile_items:
            return
        
        item = self._acquisition_tile_items[key]
        
        try:
            import shiboken6
            if not shiboken6.isValid(item):
                return
        except ImportError:
            pass
        
        try:
            # Acquired: solid green
            item.setPen(QPen(QColor(50, 200, 80, 220), 2))
            item.setBrush(QBrush(QColor(50, 200, 80, 100)))
        except RuntimeError:
            pass
    
    def _clear_region_mfov_items(self, region_idx: int):
        """Clear MFOV items for a specific region"""
        if not hasattr(self, '_acquisition_tile_items'):
            return
        
        to_remove = [k for k in self._acquisition_tile_items.keys() if k[0] == region_idx]
        for key in to_remove:
            item = self._acquisition_tile_items.pop(key, None)
            if item and item.scene() == self.scene:
                try:
                    self.scene.removeItem(item)
                except:
                    pass
    
    def clear_all_acquisition_mfovs(self):
        """Clear all acquisition MFOV visualizations"""
        if not hasattr(self, '_acquisition_tile_items'):
            return
        
        for item in self._acquisition_tile_items.values():
            if item and item.scene() == self.scene:
                try:
                    self.scene.removeItem(item)
                except:
                    pass
        self._acquisition_tile_items.clear()
    
    def _get_tiles_for_region(self, points_px: list):
        """
        Get tile positions from API.
        
        Args:
            points_px: Polygon vertices in PIXEL coordinates
            
        Returns:
            List of tile positions in µm coordinates, or empty list if API fails
        """
        if len(points_px) < 3:
            return []
        
        try:
            # Try to use API
            if hasattr(self, 'engine') and self.engine and self.engine.is_connected():
                from msem.roi_acquisition import ROIAcquisition, ROIConfig, TravelPattern, TravelDirection, StartPosition
                
                conn = self.engine.get_msem_connection()
                if conn:
                    roi_acq = ROIAcquisition(conn)
                    
                    # Get parameters from parent acquisition page
                    parent = self.parent()
                    while parent and not isinstance(parent, AcquisitionPage):
                        parent = parent.parent()
                    
                    if parent:
                        overlap_pct = parent.spin_overlap_mfov.value() / 100.0
                        sfov_overlap = parent.spin_overlap_sfov.value()
                        pixelsize = parent.spin_pixelsize.value()
                        
                        # Get travel parameters from parent's UI
                        travel_pattern = parent._get_travel_pattern()
                        travel_direction = parent._get_travel_direction()
                        start_position = parent._get_start_position()
                        
                        print(f"[DEBUG ROI] outer_mfov_overlap={overlap_pct:.3f} ({parent.spin_overlap_mfov.value()}%)")
                        print(f"[DEBUG ROI] inner_sfov_overlap={sfov_overlap} µm")
                        print(f"[DEBUG ROI] pixelsize={pixelsize} nm")
                        print(f"[DEBUG ROI] travel_pattern={travel_pattern}")
                        print(f"[DEBUG ROI] travel_direction={travel_direction}")
                        print(f"[DEBUG ROI] start_position={start_position}")
                        
                        config = ROIConfig(
                            outer_mfov_overlap=overlap_pct,
                            inner_sfov_overlap=sfov_overlap,
                            pixelsize=pixelsize,
                            travel_pattern=travel_pattern,
                            travel_direction=travel_direction,
                            start_position=start_position,
                        )
                        roi_acq.set_config(config)
                        
                        # Verify parameters were applied correctly
                        actual_hex_overlap = conn.get_double("AP_HEXAGON_OVERLAP")
                        actual_overlap_x = conn.get_double("AP_OVERLAP_X")
                        actual_overlap_y = conn.get_double("AP_OVERLAP_Y")
                        actual_pixelsize = conn.get_double("AP_PIXELSIZE")
                        print(f"[DEBUG ROI] Readback from API:")
                        print(f"  AP_HEXAGON_OVERLAP: {actual_hex_overlap:.4f} (expected {overlap_pct:.4f})")
                        print(f"  AP_OVERLAP_X: {actual_overlap_x:.2f} µm (expected {sfov_overlap:.2f} µm)")
                        print(f"  AP_OVERLAP_Y: {actual_overlap_y:.2f} µm (expected {sfov_overlap:.2f} µm)")
                        print(f"  AP_PIXELSIZE: {actual_pixelsize:.1f} nm (expected {pixelsize:.1f} nm)")
                        
                        # IMPORTANT: Convert pixel coordinates to µm for API
                        points_um = [self.pixel_to_um(px, py) for px, py in points_px]
                        roi_acq.define_roi(points_um)
                        
                        tiles = roi_acq.calculate_tiles()
                        print(f"[TileMapView] Got {len(tiles)} tiles from API")
                        return tiles
        except Exception as e:
            print(f"[TileMapView] Failed to get tiles from API: {e}")
        
        # Fallback: return empty list (no grid in simulation mode)
        print("[TileMapView] Running in simulation mode - MFOV grid not available")
        return []
    
    def draw_tile_at_position(self, x_um: float, y_um: float, size_um: float, 
                              color: QColor = None, tile_id: str = None) -> 'QGraphicsEllipseItem':
        """
        Draw a tile marker at the specified position (in µm coordinates).
        
        Args:
            x_um, y_um: Tile center position in micrometers
            size_um: Tile diameter in micrometers
            color: Fill color (default: semi-transparent red)
            tile_id: Optional identifier for the tile
        
        Returns:
            QGraphicsEllipseItem representing the tile
        """
        from PySide6.QtWidgets import QGraphicsEllipseItem
        from PySide6.QtGui import QPen, QBrush
        
        if color is None:
            color = QColor(255, 0, 0, 80)  # Semi-transparent red
        
        # Convert µm to pixel coordinates
        px, py = self.um_to_pixel(x_um, y_um)
        
        # Calculate size in pixels
        size_px = size_um / self.pixel_size_um if self.pixel_size_um > 0 else size_um
        radius_px = size_px / 2.0
        
        # Create ellipse (circle)
        tile_item = QGraphicsEllipseItem(
            px - radius_px, py - radius_px,
            size_px, size_px
        )
        tile_item.setPen(QPen(color.darker(120), 2))
        tile_item.setBrush(QBrush(color))
        tile_item.setZValue(40)  # Below ROI polygons
        
        # Store tile_id as custom data
        if tile_id:
            tile_item.setData(0, tile_id)
        
        self.scene.addItem(tile_item)
        
        return tile_item
    
    def update_tile_color(self, tile_item: 'QGraphicsEllipseItem', color: QColor):
        """Update the color of a tile marker"""
        from PySide6.QtGui import QPen, QBrush
        
        try:
            import shiboken6
            if not shiboken6.isValid(tile_item):
                return
        except ImportError:
            pass
        
        try:
            tile_item.setPen(QPen(color.darker(120), 2))
            tile_item.setBrush(QBrush(color))
        except RuntimeError:
            pass  # Object was deleted
    
    def update_support_point_color(self, region_idx: int, point_idx: int, color: QColor):
        """Update the color of a support point marker"""
        from PySide6.QtGui import QPen, QBrush
        
        if region_idx not in self._support_point_items:
            return
        
        items = self._support_point_items[region_idx]
        if point_idx < 0 or point_idx >= len(items):
            return
        
        item = items[point_idx]
        
        try:
            import shiboken6
            if not shiboken6.isValid(item):
                return
        except ImportError:
            pass
        
        try:
            item.setPen(QPen(color.darker(120), 3))
            item.setBrush(QBrush(color))
        except RuntimeError:
            pass  # Object was deleted
    
    def _load_czi_pylibczirw(self, filepath: str):
        """
        Load CZI file using pylibCZIrw (Zeiss's official library).
        Works well on Windows.
        """
        from pylibCZIrw import czi as pylibczi
        import numpy as np
        from PySide6.QtGui import QImage, QPixmap
        from PySide6.QtWidgets import QApplication
        import xml.etree.ElementTree as ET
        
        print("DEBUG: Loading with pylibCZIrw...", flush=True)
        
        pixel_size_um = 1.0
        scene_center_x_um = 0.0
        scene_center_y_um = 0.0
        
        with pylibczi.open_czi(filepath) as czidoc:
            # Get bounding box
            bbox = czidoc.total_bounding_box
            print(f"DEBUG: pylibCZIrw bounding box: {bbox}", flush=True)
            
            # Extract dimensions
            x_range = bbox.get('X', (0, 1000))
            y_range = bbox.get('Y', (0, 1000))
            width = x_range[1] - x_range[0]
            height = y_range[1] - y_range[0]
            
            print(f"DEBUG: Image dimensions: {width}x{height}", flush=True)
            QApplication.processEvents()
            
            # Get metadata for pixel size
            try:
                meta = czidoc.metadata
                if meta:
                    print(f"DEBUG: Metadata type: {type(meta).__name__}", flush=True)
                    
                    if isinstance(meta, dict):
                        # pylibCZIrw returns metadata as dict
                        # Navigate to scaling info
                        try:
                            scaling = meta.get('ImageDocument', {}).get('Metadata', {}).get('Scaling', {}).get('Items', {}).get('Distance', [])
                            if isinstance(scaling, list):
                                for dist in scaling:
                                    if dist.get('@Id') == 'X':
                                        value = dist.get('Value')
                                        if value:
                                            pixel_size_um = float(value) * 1e6
                                            print(f"DEBUG: Pixel size: {pixel_size_um:.6f} um/pixel", flush=True)
                                            break
                            elif isinstance(scaling, dict) and scaling.get('@Id') == 'X':
                                value = scaling.get('Value')
                                if value:
                                    pixel_size_um = float(value) * 1e6
                                    print(f"DEBUG: Pixel size: {pixel_size_um:.6f} um/pixel", flush=True)
                        except Exception as e2:
                            print(f"DEBUG: Could not extract pixel size from dict: {e2}", flush=True)
                    elif isinstance(meta, str):
                        # Parse XML string
                        root = ET.fromstring(meta)
                        for distance in root.findall(".//Distance[@Id='X']"):
                            value_elem = distance.find('Value')
                            if value_elem is not None and value_elem.text:
                                pixel_size_um = float(value_elem.text) * 1e6
                                print(f"DEBUG: Pixel size: {pixel_size_um:.6f} um/pixel", flush=True)
                                break
            except Exception as e:
                print(f"DEBUG: Metadata extraction failed: {e}", flush=True)
            
            # Read the image data
            print(f"Loading image {width}x{height}...", flush=True)
            QApplication.processEvents()
            
            # Read with plane specification - pylibCZIrw.read() returns just the array
            try:
                # Try reading with C=0
                img = czidoc.read(plane={'C': 0}, zoom=1.0)
                if isinstance(img, tuple):
                    img = img[0]  # Some versions return tuple
                print(f"DEBUG: Read image shape: {img.shape}", flush=True)
            except Exception as e:
                print(f"DEBUG: Read with C=0 failed: {e}, trying without plane spec", flush=True)
                try:
                    img = czidoc.read(zoom=1.0)
                    if isinstance(img, tuple):
                        img = img[0]
                    print(f"DEBUG: Read image shape: {img.shape}", flush=True)
                except Exception as e2:
                    print(f"DEBUG: Read without plane spec also failed: {e2}", flush=True)
                    # Try basic read
                    img = czidoc.read()
                    if isinstance(img, tuple):
                        img = img[0]
                    print(f"DEBUG: Basic read shape: {img.shape}", flush=True)
            
            QApplication.processEvents()
            
            # Process the image
            img = np.squeeze(img)
            print(f"DEBUG: After squeeze: {img.shape}", flush=True)
            
            # Handle different dimension orders
            if len(img.shape) == 3:
                if img.shape[0] <= 4:
                    # (C, H, W) -> take first channel
                    img = img[0]
                elif img.shape[2] <= 4:
                    # (H, W, C) -> take first channel
                    img = img[:, :, 0]
            
            print(f"DEBUG: Final shape: {img.shape}", flush=True)
            
            orig_h, orig_w = img.shape[:2]
            
            # Normalize to uint8
            if img.dtype != np.uint8:
                pmin, pmax = np.percentile(img, (1, 99))
                img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
            
            # Create QImage
            img = np.ascontiguousarray(img)
            qimg = QImage(img.data, orig_w, orig_h, orig_w, QImage.Format_Grayscale8)
            
            print(f"CZI loaded (pylibCZIrw): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px", flush=True)
            
            return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um, (0.0, 0.0)
    
    def _load_czi_full(self, filepath: str):
        """
        Load CZI file at FULL resolution (no subsampling).
        Handles both regular and mosaic CZI files.
        
        Returns:
            (QPixmap, (width, height), pixel_size_um, (offset_x_um, offset_y_um)) 
            - pixmap, original size tuple, pixel size, and offset in um
        """
        import sys
        print(f"DEBUG: _load_czi_full called with: {filepath}", flush=True)
        sys.stdout.flush()
        
        # Try pylibCZIrw first (works better on Windows)
        try:
            return self._load_czi_pylibczirw(filepath)
        except ImportError:
            print("DEBUG: pylibCZIrw not available, trying aicspylibczi...", flush=True)
        except Exception as e:
            print(f"DEBUG: pylibCZIrw failed: {e}, trying aicspylibczi...", flush=True)
        
        # Try aicspylibczi
        try:
            print("DEBUG: Attempting to import aicspylibczi...", flush=True)
            sys.stdout.flush()
            from aicspylibczi import CziFile
            print("DEBUG: aicspylibczi imported successfully", flush=True)
            sys.stdout.flush()
            
            import numpy as np
            from PySide6.QtGui import QImage, QPixmap
            from PySide6.QtWidgets import QApplication
            
            print("DEBUG: Opening CZI file...", flush=True)
            sys.stdout.flush()
            czi = CziFile(filepath)
            print("DEBUG: CZI file opened", flush=True)
            sys.stdout.flush()
            QApplication.processEvents()
            
            # Extract pixel size and stage position from metadata
            pixel_size_um = 1.0  # Default
            scene_center_x_um = 0.0  # Scene center X in um
            scene_center_y_um = 0.0  # Scene center Y in um
            try:
                import xml.etree.ElementTree as ET
                metadata = czi.meta
                
                # Handle different types of metadata
                # czi.meta can return an Element, ElementTree, or string
                if hasattr(metadata, 'tag'):
                    # It's an Element or Element-like object
                    root = metadata
                elif hasattr(metadata, 'getroot'):
                    # It's an ElementTree
                    root = metadata.getroot()
                elif isinstance(metadata, (str, bytes)):
                    root = ET.fromstring(metadata)
                else:
                    # Try to use it directly as Element
                    root = metadata
                
                print(f"DEBUG: Metadata type: {type(metadata).__name__}, root tag: {root.tag if hasattr(root, 'tag') else 'N/A'}")
                
                # Find ScalingX (pixel size in um)
                for distance in root.findall(".//Distance[@Id='X']"):
                    value_elem = distance.find('Value')
                    if value_elem is not None:
                        pixel_size_um = float(value_elem.text) * 1e6  # Convert m to um
                        print(f"DEBUG: Pixel size from metadata: {pixel_size_um:.6f} um/pixel")
                        break
                
                # Try to find scene center position from metadata
                # Format: <CenterPosition>112915.39,100841.826</CenterPosition>
                for scene in root.findall(".//Scene"):
                    center = scene.find("CenterPosition")
                    if center is not None and center.text:
                        # Parse comma-separated values (already in um for Zeiss files)
                        coords = center.text.split(',')
                        if len(coords) >= 2:
                            scene_center_x_um = float(coords[0].strip())
                            scene_center_y_um = float(coords[1].strip())
                            print(f"DEBUG: Scene center position: ({scene_center_x_um:.2f}, {scene_center_y_um:.2f}) um")
                        break
                        
            except Exception as e:
                print(f"DEBUG: Could not extract metadata: {e}")
                import traceback
                traceback.print_exc()
            
            # Offset in pixels (will be converted to um)
            offset_x_px = 0
            offset_y_px = 0
            
            # Check if this is a mosaic file
            if czi.is_mosaic():
                # Mosaic CZI - use read_mosaic
                bbox = czi.get_mosaic_bounding_box()
                full_x, full_y = bbox.w, bbox.h
                
                # Store the bounding box offset (position of cropped region in original coordinates)
                offset_x_px = bbox.x
                offset_y_px = bbox.y
                print(f"DEBUG: Mosaic bounding box: x={bbox.x}, y={bbox.y}, w={bbox.w}, h={bbox.h}")
                
                # Calculate image size in megapixels to decide on scale factor
                megapixels = (full_x * full_y) / 1e6
                print(f"DEBUG: Image size: {megapixels:.1f} MP")
                
                # Use scale factor for very large images to prevent memory issues
                # > 500 MP: use 0.25, > 200 MP: use 0.5, otherwise: 1.0
                if megapixels > 500:
                    scale_factor = 0.25
                    print(f"WARNING: Very large image ({megapixels:.0f} MP), using scale factor 0.25")
                elif megapixels > 200:
                    scale_factor = 0.5
                    print(f"WARNING: Large image ({megapixels:.0f} MP), using scale factor 0.5")
                else:
                    scale_factor = 1.0
                
                print(f"Loading mosaic {full_x}x{full_y} (scale={scale_factor})...")
                QApplication.processEvents()
                
                # Get dimensions to determine C (channel) count
                dims = czi.get_dims_shape()[0]
                c_count = dims.get('C', (0, 1))[1] if isinstance(dims.get('C'), tuple) else dims.get('C', 1)
                print(f"DEBUG: CZI has {c_count} channels")
                
                QApplication.processEvents()
                
                # Read mosaic with scale factor - must specify C dimension
                mosaic = None
                try:
                    # Read with region and C=0
                    mosaic = czi.read_mosaic(region=(bbox.x, bbox.y, bbox.w, bbox.h), scale_factor=scale_factor, C=0)
                    print(f"DEBUG: Read mosaic with explicit region and C=0: {mosaic.shape}")
                except Exception as e1:
                    print(f"DEBUG: First attempt failed: {e1}")
                    QApplication.processEvents()
                    try:
                        # Fallback: read without region but with C=0
                        mosaic = czi.read_mosaic(scale_factor=scale_factor, C=0)
                        print(f"DEBUG: Read mosaic with C=0: {mosaic.shape}")
                    except Exception as e2:
                        print(f"DEBUG: Second attempt failed: {e2}")
                        QApplication.processEvents()
                        # Final fallback: try reading all channels
                        mosaic = czi.read_mosaic(scale_factor=scale_factor)
                        print(f"DEBUG: Read mosaic without C specification: {mosaic.shape}")
                
                if mosaic is None:
                    raise RuntimeError("Failed to read mosaic data")
                
                # Adjust pixel size for scale factor
                pixel_size_um = pixel_size_um / scale_factor
                
                QApplication.processEvents()
                print("Processing...")
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
                
                print(f"Loading {full_x}x{full_y}...")
                QApplication.processEvents()
                
                img, _ = czi.read_image()
                
                QApplication.processEvents()
                print("Processing...")
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
            
            # Convert RGB to Grayscale if needed (average of channels or use first channel)
            if len(img.shape) == 3 and img.shape[2] >= 3:
                # Use luminosity method: 0.299*R + 0.587*G + 0.114*B
                # Or just take the first channel for simplicity (often they're all the same)
                print(f"DEBUG: Converting RGB image to grayscale")
                img = img[:, :, 0]  # Take first channel
            
            # Normalize with percentile clipping for better contrast
            if img.dtype != np.uint8:
                pmin, pmax = np.percentile(img, (1, 99))
                img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
            
            # Create QImage (always grayscale now)
            h, w = img.shape
            img = np.ascontiguousarray(img)
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
            print(f"CZI loaded (full, Grayscale): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
            
            # Calculate image size in um
            image_width_um = orig_w * pixel_size_um
            image_height_um = orig_h * pixel_size_um
            print(f"DEBUG: Image size: {image_width_um:.2f} x {image_height_um:.2f} um")
            
            # The scene center position is where the image center is in stage coordinates
            # This is crucial for aligning with ROIs that are defined in stage coordinates
            print(f"DEBUG: Scene center position: ({scene_center_x_um:.2f}, {scene_center_y_um:.2f}) um")
            
            # Return pixmap, size, pixel_size_um, and scene center (for calibration offset)
            return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um, (scene_center_x_um, scene_center_y_um)
            
        except ImportError as ie:
            # aicspylibczi not available - try pylibczirw first, then czifile
            print(f"DEBUG: aicspylibczi not available ({ie})")
            
            # Try pylibczirw as second option (Zeiss's own library)
            try:
                from pylibCZIrw import czi as pylibczi
                import numpy as np
                from PySide6.QtGui import QImage, QPixmap
                from PySide6.QtWidgets import QApplication
                
                print("Loading (pylibCZIrw)...")
                QApplication.processEvents()
                
                with pylibczi.open_czi(filepath) as czidoc:
                    # Get dimensions
                    dims = czidoc.total_bounding_box
                    width = dims['X'][1] - dims['X'][0]
                    height = dims['Y'][1] - dims['Y'][0]
                    
                    print(f"DEBUG: pylibCZIrw dimensions: {width}x{height}")
                    
                    # Read the full image
                    img, _ = czidoc.read(plane={'C': 0})
                    img = np.squeeze(img)
                    
                    print(f"DEBUG: pylibCZIrw image shape: {img.shape}")
                    
                    # Handle dimensions
                    if len(img.shape) == 3:
                        if img.shape[0] <= 4:
                            img = img[0]  # (C, H, W) -> (H, W)
                        elif img.shape[2] <= 4:
                            img = img[:, :, 0]  # (H, W, C) -> (H, W)
                    
                    orig_h, orig_w = img.shape
                    
                    # Try to get pixel size from metadata
                    pixel_size_um = 1.0
                    try:
                        meta = czidoc.metadata
                        # Look for scaling info
                        if hasattr(meta, 'scaling'):
                            pixel_size_um = meta.scaling.x * 1e6
                    except:
                        pass
                    
                    # Normalize
                    if img.dtype != np.uint8:
                        pmin, pmax = np.percentile(img, (1, 99))
                        img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
                    
                    img = np.ascontiguousarray(img)
                    qimg = QImage(img.data, orig_w, orig_h, orig_w, QImage.Format_Grayscale8)
                    
                    print(f"CZI loaded (pylibCZIrw): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
                    return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um, (0.0, 0.0)
                    
            except ImportError:
                pass  # pylibczirw not available, continue to czifile
            except Exception as e:
                print(f"DEBUG: pylibCZIrw failed: {e}")
            
            # Final fallback: czifile
            print("WARNING: For mosaic CZI files, install: pip install aicspylibczi")
            print("WARNING: Or try: pip install pylibCZIrw")
            print("WARNING: Falling back to czifile (may not handle mosaic files correctly)")
            
            try:
                import czifile
                import numpy as np
                from PySide6.QtGui import QImage, QPixmap
                from PySide6.QtWidgets import QApplication
                
                print("Loading (czifile - fallback mode)...")
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
                
                print(f"DEBUG: czifile raw shape: {img.shape}, dtype: {img.dtype}")
                
                # czifile returns data in TZCYX or similar order
                # For mosaic files, we need to find the actual H and W dimensions
                # which are typically the two largest dimensions
                
                # First squeeze out singleton dimensions
                img = np.squeeze(img)
                print(f"DEBUG: After squeeze: {img.shape}")
                
                # Find the two largest dimensions (these should be H and W)
                if len(img.shape) > 2:
                    # Get dimension sizes and indices
                    dim_sizes = [(i, s) for i, s in enumerate(img.shape)]
                    # Sort by size, largest first
                    dim_sizes.sort(key=lambda x: x[1], reverse=True)
                    
                    # The two largest dimensions are likely H and W
                    largest_dims = [dim_sizes[0][0], dim_sizes[1][0]]
                    largest_dims.sort()  # Keep order (smaller index first)
                    
                    print(f"DEBUG: Largest dims at indices {largest_dims}: {[img.shape[d] for d in largest_dims]}")
                    
                    # Reduce other dimensions by taking first element
                    # Start from highest dimension to avoid index shifting
                    for i in range(len(img.shape) - 1, -1, -1):
                        if i not in largest_dims:
                            img = np.take(img, 0, axis=i)
                            img = np.squeeze(img)
                    
                    print(f"DEBUG: After reducing to 2D: {img.shape}")
                
                # Ensure 2D
                while len(img.shape) > 2:
                    img = img[0]
                
                print(f"DEBUG: Final czifile image shape: {img.shape}")
                
                if len(img.shape) != 2:
                    raise ValueError(f"Could not reduce image to 2D, got shape {img.shape}")
                
                orig_h, orig_w = img.shape
                
                if img.dtype != np.uint8:
                    pmin, pmax = np.percentile(img, (1, 99))
                    img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
                
                h, w = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
                
                print(f"CZI loaded (czifile, full): {orig_w}x{orig_h}, pixel_size={pixel_size_um:.6f} um/px")
                
                # czifile doesn't provide offset info, use (0, 0)
                return QPixmap.fromImage(qimg.copy()), (orig_w, orig_h), pixel_size_um, (0.0, 0.0)
                
            except Exception as e:
                print(f"CZI load error (czifile): {e}")
                return None, None, 1.0, (0.0, 0.0)
                
        except Exception as e:
            print(f"CZI load error: {e}")
            import traceback
            traceback.print_exc()
            return None, None, 1.0, (0.0, 0.0)


class AcquisitionPage(BasePage):
    """Page for ROI acquisition with tiles setup"""
    
    def setup_ui(self):
        main_split = QSplitter(Qt.Horizontal)
        
        # === Left: Settings Panel ===
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(320)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setAlignment(Qt.AlignTop)
        left_layout.setSpacing(8)
        
        # --- Image Setup ---
        image_box = CollapsibleBox("Image Setup")
        image_box.toggle()  # Start expanded
        image_content = QVBoxLayout()
        
        img_layout = QHBoxLayout()
        self.btn_load_image = QPushButton("Load Image...")
        self.btn_load_image.clicked.connect(self._on_load_image)
        
        self.btn_clear_image = QPushButton("Clear")
        self.btn_clear_image.setStyleSheet("background-color: #5a5a5a;")
        self.btn_clear_image.setFixedWidth(50)
        self.btn_clear_image.clicked.connect(self._on_clear_map)
        
        img_layout.addWidget(self.btn_load_image)
        img_layout.addWidget(self.btn_clear_image)
        
        self.lbl_image_info = QLabel("No image loaded")
        self.lbl_image_info.setStyleSheet("color: #666; font-style: italic; font-size: 10px;")
        
        image_content.addLayout(img_layout)
        image_content.addWidget(self.lbl_image_info)
        
        # Manual offset adjustment for aligning image with ROIs
        offset_label = QLabel("Image Offset (um):")
        offset_label.setStyleSheet("color: #888; font-size: 10px; margin-top: 8px;")
        image_content.addWidget(offset_label)
        
        offset_layout = QHBoxLayout()
        offset_layout.addWidget(QLabel("X:"))
        self.spin_offset_x = QDoubleSpinBox()
        self.spin_offset_x.setRange(-50000, 50000)
        self.spin_offset_x.setValue(0)
        self.spin_offset_x.setSingleStep(100)
        self.spin_offset_x.setSuffix(" um")
        self.spin_offset_x.setToolTip("Positive = shift ROIs left (image right)")
        self.spin_offset_x.valueChanged.connect(self._on_offset_changed)
        offset_layout.addWidget(self.spin_offset_x)
        
        offset_layout.addWidget(QLabel("Y:"))
        self.spin_offset_y = QDoubleSpinBox()
        self.spin_offset_y.setRange(-50000, 50000)
        self.spin_offset_y.setValue(0)
        self.spin_offset_y.setSingleStep(100)
        self.spin_offset_y.setSuffix(" um")
        self.spin_offset_y.setToolTip("Positive = shift ROIs down (image up)")
        self.spin_offset_y.valueChanged.connect(self._on_offset_changed)
        offset_layout.addWidget(self.spin_offset_y)
        
        image_content.addLayout(offset_layout)
        
        image_box.set_content_layout(image_content)
        left_layout.addWidget(image_box)
        
        # --- Stage Control ---
        stage_box = CollapsibleBox("Stage Control")
        stage_content = QVBoxLayout()
        
        # Current position display
        self.lbl_stage_pos = QLabel("Stage: X=0.0 Y=0.0 Z=0.0 µm")
        self.lbl_stage_pos.setStyleSheet("font-family: monospace; font-size: 11px; color: #4ec9b0;")
        stage_content.addWidget(self.lbl_stage_pos)
        
        # Operation status
        self.lbl_operation = QLabel("Status: Idle")
        self.lbl_operation.setStyleSheet("font-family: monospace; font-size: 10px; color: #888;")
        stage_content.addWidget(self.lbl_operation)
        
        # Navigation grid (3x3)
        nav_grid = QHBoxLayout()
        nav_grid.setSpacing(2)
        
        # Step size
        step_layout = QHBoxLayout()
        step_layout.addWidget(QLabel("Step:"))
        self.spin_step = QDoubleSpinBox()
        self.spin_step.setRange(1, 10000)
        self.spin_step.setValue(100)
        self.spin_step.setSuffix(" µm")
        self.spin_step.setFixedWidth(100)
        step_layout.addWidget(self.spin_step)
        step_layout.addStretch()
        stage_content.addLayout(step_layout)
        
        # Navigation buttons (5 button layout: ↑ center, ← ● → middle row, ↓ bottom)
        from PySide6.QtWidgets import QGridLayout
        nav_grid = QGridLayout()
        nav_grid.setSpacing(2)
        
        btn_up = QPushButton("↑")
        btn_down = QPushButton("↓")
        btn_left = QPushButton("←")
        btn_right = QPushButton("→")
        btn_center = QPushButton("●")
        
        for btn in [btn_up, btn_down, btn_left, btn_right, btn_center]:
            btn.setFixedSize(40, 40)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #3e3e3e;
                    border: 1px solid #555;
                    border-radius: 4px;
                    font-size: 16px;
                    font-weight: bold;
                }
                QPushButton:hover { background-color: #505050; }
                QPushButton:pressed { background-color: #094771; }
            """)
        
        btn_center.setToolTip("Go to current crosshair position")
        btn_up.setToolTip("Move stage up (Y+)")
        btn_down.setToolTip("Move stage down (Y-)")
        btn_left.setToolTip("Move stage left (X-)")
        btn_right.setToolTip("Move stage right (X+)")
        
        btn_up.clicked.connect(lambda: self._move_stage_relative(0, self.spin_step.value()))
        btn_down.clicked.connect(lambda: self._move_stage_relative(0, -self.spin_step.value()))
        btn_left.clicked.connect(lambda: self._move_stage_relative(-self.spin_step.value(), 0))
        btn_right.clicked.connect(lambda: self._move_stage_relative(self.spin_step.value(), 0))
        btn_center.clicked.connect(self._move_stage_to_crosshair)
        
        nav_grid.addWidget(btn_up, 0, 1)
        nav_grid.addWidget(btn_left, 1, 0)
        nav_grid.addWidget(btn_center, 1, 1)
        nav_grid.addWidget(btn_right, 1, 2)
        nav_grid.addWidget(btn_down, 2, 1)
        
        stage_content.addLayout(nav_grid)
        
        # Go to position
        goto_layout = QHBoxLayout()
        goto_layout.addWidget(QLabel("Go to:"))
        self.spin_goto_x = QDoubleSpinBox()
        self.spin_goto_x.setRange(-200000, 200000)
        self.spin_goto_x.setDecimals(1)
        self.spin_goto_x.setSuffix(" X")
        self.spin_goto_x.setFixedWidth(85)
        
        self.spin_goto_y = QDoubleSpinBox()
        self.spin_goto_y.setRange(-200000, 200000)
        self.spin_goto_y.setDecimals(1)
        self.spin_goto_y.setSuffix(" Y")
        self.spin_goto_y.setFixedWidth(85)
        
        self.btn_goto = QPushButton("Go")
        self.btn_goto.setFixedWidth(40)
        self.btn_goto.clicked.connect(self._on_goto_position)
        
        goto_layout.addWidget(self.spin_goto_x)
        goto_layout.addWidget(self.spin_goto_y)
        goto_layout.addWidget(self.btn_goto)
        stage_content.addLayout(goto_layout)
        
        # Read position button
        self.btn_read_pos = QPushButton("📍 Read Position")
        self.btn_read_pos.setToolTip("Read current position from hardware")
        self.btn_read_pos.clicked.connect(self._on_read_position)
        stage_content.addWidget(self.btn_read_pos)
        
        stage_box.set_content_layout(stage_content)
        left_layout.addWidget(stage_box)
        
        # --- Tiles (Regions) ---
        tiles_box = CollapsibleBox("Tiles")
        tiles_box.toggle()  # Start expanded
        tiles_content = QVBoxLayout()
        
        # Regions section header with lock button
        regions_header = QHBoxLayout()
        regions_label = QLabel("Regions:")
        regions_label.setStyleSheet("font-weight: bold;")
        regions_header.addWidget(regions_label)
        regions_header.addStretch()
        
        # Lock/Unlock button for mask editing
        self.btn_lock_masks = QPushButton("🔒 Locked")
        self.btn_lock_masks.setCheckable(True)
        self.btn_lock_masks.setChecked(True)  # Start locked
        self.btn_lock_masks.setFixedWidth(80)
        self.btn_lock_masks.setToolTip("Click to unlock masks for moving/rotating")
        self.btn_lock_masks.setStyleSheet("""
            QPushButton {
                background-color: #3e3e3e;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 2px 6px;
                font-size: 11px;
            }
            QPushButton:checked {
                background-color: #2d5a2d;
            }
            QPushButton:!checked {
                background-color: #5a2d2d;
            }
        """)
        self.btn_lock_masks.clicked.connect(self._on_lock_toggle)
        regions_header.addWidget(self.btn_lock_masks)
        
        # Select All / Deselect All buttons
        self.btn_select_all = QPushButton("All")
        self.btn_select_all.setFixedWidth(35)
        self.btn_select_all.setToolTip("Enable all regions")
        self.btn_select_all.clicked.connect(self._on_select_all_regions)
        
        self.btn_deselect_all = QPushButton("None")
        self.btn_deselect_all.setFixedWidth(40)
        self.btn_deselect_all.setToolTip("Disable all regions")
        self.btn_deselect_all.clicked.connect(self._on_deselect_all_regions)
        
        regions_header.addWidget(self.btn_select_all)
        regions_header.addWidget(self.btn_deselect_all)
        
        # Regions list with checkboxes
        self.regions_list = QListWidget()
        self.regions_list.setMaximumHeight(150)
        self.regions_list.setStyleSheet("""
            QListWidget {
                background-color: #2a2a2a;
                border: 1px solid #3e3e3e;
                border-radius: 4px;
            }
            QListWidget::item {
                padding: 4px;
                border-bottom: 1px solid #3e3e3e;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
        """)
        self.regions_list.itemClicked.connect(self._on_region_selected)
        self.regions_list.itemChanged.connect(self._on_region_checkbox_changed)
        
        # Draw buttons
        draw_layout = QHBoxLayout()
        self.btn_draw_polygon = QPushButton("Draw Polygon")
        self.btn_draw_polygon.setToolTip("Click to draw polygon regions on the map")
        self.btn_draw_polygon.clicked.connect(self._on_start_drawing)
        
        self.btn_load_roi = QPushButton("Load")
        self.btn_load_roi.setToolTip("Load regions from CSV or JSON file")
        self.btn_load_roi.clicked.connect(self._on_load_roi)
        
        self.btn_save_roi = QPushButton("💾 Save")
        self.btn_save_roi.setToolTip("Save regions and support points to JSON file")
        self.btn_save_roi.clicked.connect(self._on_save_roi)
        
        draw_layout.addWidget(self.btn_draw_polygon)
        draw_layout.addWidget(self.btn_load_roi)
        draw_layout.addWidget(self.btn_save_roi)
        
        # Apply button (only visible when drawing)
        self.btn_apply_drawing = QPushButton("✓ Apply")
        self.btn_apply_drawing.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #128a12;
            }
        """)
        self.btn_apply_drawing.clicked.connect(self._on_apply_drawing)
        self.btn_apply_drawing.hide()
        
        # Drawing status
        self.lbl_drawing_status = QLabel("")
        self.lbl_drawing_status.setStyleSheet("color: #888; font-size: 10px;")
        self.lbl_drawing_status.hide()
        
        # Clear and delete buttons
        action_layout = QHBoxLayout()
        self.btn_delete_region = QPushButton("Delete")
        self.btn_delete_region.setStyleSheet("background-color: #5a5a5a;")
        self.btn_delete_region.clicked.connect(self._on_delete_region)
        
        self.btn_clear_roi = QPushButton("Clear All")
        self.btn_clear_roi.setStyleSheet("background-color: #5a5a5a;")
        self.btn_clear_roi.clicked.connect(self._on_clear_rois)
        
        action_layout.addWidget(self.btn_delete_region)
        action_layout.addWidget(self.btn_clear_roi)
        
        tiles_content.addLayout(regions_header)
        tiles_content.addWidget(self.regions_list)
        tiles_content.addLayout(draw_layout)
        tiles_content.addWidget(self.btn_apply_drawing)
        tiles_content.addWidget(self.lbl_drawing_status)
        tiles_content.addLayout(action_layout)
        
        tiles_box.set_content_layout(tiles_content)
        left_layout.addWidget(tiles_box)
        
        # --- Support Points ---
        sp_box = CollapsibleBox("Support Points (Autofocus)")
        sp_content = QVBoxLayout()
        
        # Info label
        sp_info = QLabel("Define autofocus points within selected region")
        sp_info.setStyleSheet("color: #888; font-size: 10px;")
        sp_content.addWidget(sp_info)
        
        # Number of support points
        sp_count_layout = QHBoxLayout()
        sp_count_layout.addWidget(QLabel("Count:"))
        self.spin_support_points = QSpinBox()
        self.spin_support_points.setRange(1, 50)
        self.spin_support_points.setValue(5)
        self.spin_support_points.setToolTip("Number of autofocus support points")
        sp_count_layout.addWidget(self.spin_support_points)
        sp_count_layout.addStretch()
        sp_content.addLayout(sp_count_layout)
        
        # Apply button
        self.btn_apply_support_points = QPushButton("🎯 Apply to Selected Region")
        self.btn_apply_support_points.setToolTip("Distribute support points in selected region")
        self.btn_apply_support_points.clicked.connect(self._on_apply_support_points)
        sp_content.addWidget(self.btn_apply_support_points)
        
        # Clear support points button
        self.btn_clear_support_points = QPushButton("Clear Support Points")
        self.btn_clear_support_points.setStyleSheet("background-color: #5a5a5a;")
        self.btn_clear_support_points.clicked.connect(self._on_clear_support_points)
        sp_content.addWidget(self.btn_clear_support_points)
        
        # Coordinates table
        sp_table_label = QLabel("Support Point Coordinates:")
        sp_table_label.setStyleSheet("color: #888; font-size: 10px; margin-top: 10px;")
        sp_content.addWidget(sp_table_label)
        
        self.support_points_table = QTableWidget()
        self.support_points_table.setColumnCount(3)
        self.support_points_table.setHorizontalHeaderLabels(["Point #", "X (µm)", "Y (µm)"])
        self.support_points_table.setMaximumHeight(200)
        self.support_points_table.setAlternatingRowColors(True)
        self.support_points_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.support_points_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.support_points_table.horizontalHeader().setStretchLastSection(True)
        self.support_points_table.setStyleSheet("""
            QTableWidget {
                background-color: #2d2d2d;
                gridline-color: #555;
                color: #cccccc;
            }
            QTableWidget::item:alternate {
                background-color: #333;
            }
            QHeaderView::section {
                background-color: #3e3e3e;
                color: #cccccc;
                padding: 4px;
                border: 1px solid #555;
            }
        """)
        sp_content.addWidget(self.support_points_table)
        
        # Status label
        self.lbl_support_points_status = QLabel("No region selected")
        self.lbl_support_points_status.setStyleSheet("color: #888; font-size: 10px;")
        sp_content.addWidget(self.lbl_support_points_status)
        
        sp_box.set_content_layout(sp_content)
        left_layout.addWidget(sp_box)
        
        # --- ROI Settings ---
        roi_box = CollapsibleBox("ROI Settings")
        roi_form = QFormLayout()
        
        self.combo_travel = QComboBox()
        self.combo_travel.addItems(["Comb", "Serpentine"])
        
        self.combo_direction = QComboBox()
        self.combo_direction.addItems(["Horizontal Up", "Horizontal Down", "Vertical Left", "Vertical Right"])
        
        self.combo_start = QComboBox()
        self.combo_start.addItems(["Bottom Left", "Bottom Right", "Top Left", "Top Right"])
        
        roi_form.addRow("Travel Pattern:", self.combo_travel)
        roi_form.addRow("Direction:", self.combo_direction)
        roi_form.addRow("Start Position:", self.combo_start)
        
        roi_box.set_content_layout(roi_form)
        left_layout.addWidget(roi_box)
        
        # --- Scan Parameters ---
        scan_box = CollapsibleBox("Scan Parameters ⚙️")
        scan_layout = QVBoxLayout()
        
        # Info label about parameter source
        param_info = QLabel("✓ Primary parameters (used for acquisition)")
        param_info.setStyleSheet("color: #4ec9b0; font-size: 10px; font-style: italic;")
        scan_layout.addWidget(param_info)
        
        scan_form = QFormLayout()
        
        # AP_HEXAGON_OVERLAP: 0.0 - 0.5 (0-50% of MFOV)
        self.spin_overlap_mfov = QDoubleSpinBox()
        self.spin_overlap_mfov.setRange(0.0, 50.0)
        self.spin_overlap_mfov.setValue(4.0)
        self.spin_overlap_mfov.setSuffix(" %")
        self.spin_overlap_mfov.setDecimals(2)
        self.spin_overlap_mfov.valueChanged.connect(self._on_param_changed)
        
        # AP_OVERLAP_X/Y: 0 - 6 µm (from API and test scripts)
        self.spin_overlap_sfov = QDoubleSpinBox()
        self.spin_overlap_sfov.setRange(0.0, 6.0)
        self.spin_overlap_sfov.setValue(1.0)
        self.spin_overlap_sfov.setSuffix(" µm")
        self.spin_overlap_sfov.setDecimals(2)
        self.spin_overlap_sfov.valueChanged.connect(self._on_param_changed)
        
        # AP_PIXELSIZE: 1 - 100 nm (from API examples)
        self.spin_pixelsize = QDoubleSpinBox()
        self.spin_pixelsize.setRange(1.0, 100.0)
        self.spin_pixelsize.setValue(4.0)
        self.spin_pixelsize.setSuffix(" nm")
        self.spin_pixelsize.setDecimals(1)
        self.spin_pixelsize.valueChanged.connect(self._on_param_changed)
        
        # Scan rate: 1-8 (from test scripts)
        self.combo_scanrate = QComboBox()
        for rate in range(1, 9):
            speed = "Fast" if rate >= 6 else ("Medium" if rate >= 3 else "Slow")
            self.combo_scanrate.addItem(f"{rate} ({speed})", rate)
        self.combo_scanrate.setCurrentIndex(0)  # Default: rate 1
        self.combo_scanrate.currentIndexChanged.connect(self._on_param_changed)
        
        scan_form.addRow("MFOV Overlap:", self.spin_overlap_mfov)
        scan_form.addRow("SFOV Overlap:", self.spin_overlap_sfov)
        scan_form.addRow("Pixel Size:", self.spin_pixelsize)
        scan_form.addRow("Scan Rate:", self.combo_scanrate)
        
        scan_layout.addLayout(scan_form)
        scan_box.set_content_layout(scan_layout)
        left_layout.addWidget(scan_box)
        
        # --- Output Settings ---
        output_box = CollapsibleBox("Output Settings")
        output_layout = QVBoxLayout()
        output_form = QFormLayout()
        
        # Output path
        path_layout = QHBoxLayout()
        self.edit_output_path = QLineEdit("V:\\Data")
        self.btn_browse = QPushButton("...")
        self.btn_browse.setFixedWidth(30)
        self.btn_browse.clicked.connect(self._browse_output)
        path_layout.addWidget(self.edit_output_path)
        path_layout.addWidget(self.btn_browse)
        
        # Prev focus-map folder: a directory of per-section prev_focusmap .txt
        # files. The pipeline matches each section to its file by number
        # (S25R1 -> w*_s25.txt). Leave blank to use the dict baked into
        # focus_map.CONFIG['prev_focusmap_by_region'].
        pfm_layout = QHBoxLayout()
        self.edit_prev_focusmap_dir = QLineEdit("")
        self.edit_prev_focusmap_dir.setPlaceholderText("(optional) folder of prev_focusmap *.txt")
        self.btn_browse_pfm = QPushButton("...")
        self.btn_browse_pfm.setFixedWidth(30)
        self.btn_browse_pfm.clicked.connect(self._browse_prev_focusmap_dir)
        pfm_layout.addWidget(self.edit_prev_focusmap_dir)
        pfm_layout.addWidget(self.btn_browse_pfm)

        # Image settings
        self.edit_prefix = QLineEdit("tile_")

        self.combo_format = QComboBox()
        self.combo_format.addItems(["bmp", "tif", "png", "jpg"])

        output_form.addRow("Output Path:", path_layout)
        output_form.addRow("Prev Focus-map Dir:", pfm_layout)
        output_form.addRow("File Prefix:", self.edit_prefix)
        output_form.addRow("Image Format:", self.combo_format)
        
        output_layout.addLayout(output_form)
        
        # Thumbnail settings section
        thumb_label = QLabel("Thumbnail Settings")
        thumb_label.setStyleSheet("font-weight: bold; color: #888; margin-top: 8px;")
        output_layout.addWidget(thumb_label)
        
        thumb_form = QFormLayout()
        
        self.chk_enable_thumbnails = QCheckBox("Enable Thumbnails")
        self.chk_enable_thumbnails.setChecked(True)
        self.chk_enable_thumbnails.stateChanged.connect(self._on_thumbnail_toggle)
        
        self.edit_thumb_prefix = QLineEdit("thumb_")
        
        self.combo_thumb_format = QComboBox()
        self.combo_thumb_format.addItems(["jpg", "png", "bmp"])
        
        self.spin_thumb_quality = QSpinBox()
        self.spin_thumb_quality.setRange(1, 100)
        self.spin_thumb_quality.setValue(85)
        self.spin_thumb_quality.setSuffix("%")
        self.spin_thumb_quality.setToolTip("JPEG quality (1-100)")
        
        thumb_form.addRow("", self.chk_enable_thumbnails)
        thumb_form.addRow("Prefix:", self.edit_thumb_prefix)
        thumb_form.addRow("Format:", self.combo_thumb_format)
        thumb_form.addRow("Quality:", self.spin_thumb_quality)
        
        output_layout.addLayout(thumb_form)
        
        # Metadata settings
        meta_label = QLabel("Metadata")
        meta_label.setStyleSheet("font-weight: bold; color: #888; margin-top: 8px;")
        output_layout.addWidget(meta_label)
        
        meta_form = QFormLayout()
        
        self.edit_experiment_name = QLineEdit("")
        self.edit_experiment_name.setPlaceholderText("Auto-generated if empty")
        
        self.edit_sample_name = QLineEdit("")
        self.edit_sample_name.setPlaceholderText("Sample identifier")
        
        self.edit_operator_name = QLineEdit("")
        self.edit_operator_name.setPlaceholderText("Operator name")
        
        meta_form.addRow("Experiment:", self.edit_experiment_name)
        meta_form.addRow("Sample:", self.edit_sample_name)
        meta_form.addRow("Operator:", self.edit_operator_name)
        
        output_layout.addLayout(meta_form)
        
        output_box.set_content_layout(output_layout)
        left_layout.addWidget(output_box)
        
        # --- Acquisition Control ---
        ctrl_box = CollapsibleBox("Acquisition Control")
        ctrl_box.toggle()  # Start expanded
        ctrl_layout = QVBoxLayout()
        
        # Tile count
        tile_info = QHBoxLayout()
        tile_info.addWidget(QLabel("Tiles:"))
        self.lbl_tile_count = QLabel("0")
        self.lbl_tile_count.setStyleSheet("font-weight: bold; color: #4ec9b0;")
        tile_info.addWidget(self.lbl_tile_count)
        tile_info.addStretch()
        
        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        
        self.lbl_progress = QLabel("Ready")
        self.lbl_progress.setStyleSheet("color: #888;")
        
        # Buttons
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("▶ Start Acquisition")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                font-weight: bold;
                padding: 12px;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #128a12; }
        """)
        self.btn_start.clicked.connect(self._on_start_acquisition)
        
        self.btn_stop = QPushButton("⬛ Stop")
        self.btn_stop.setStyleSheet("background-color: #c42b1c; font-weight: bold;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_acquisition)
        
        btn_layout.addWidget(self.btn_start, 2)
        btn_layout.addWidget(self.btn_stop, 1)
        
        ctrl_layout.addLayout(tile_info)
        ctrl_layout.addWidget(self.progress_bar)
        ctrl_layout.addWidget(self.lbl_progress)
        ctrl_layout.addLayout(btn_layout)
        
        ctrl_box.set_content_layout(ctrl_layout)
        left_layout.addWidget(ctrl_box)
        
        left_layout.addStretch()
        left_scroll.setWidget(left_widget)
        
        # === Center: Large Map View ===
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        
        # Header
        header = QHBoxLayout()
        header.addWidget(QLabel("ROI & Tile Visualization"))
        
        self.lbl_pos = QLabel("Click to move stage")
        self.lbl_pos.setStyleSheet("color: #888; font-size: 11px;")
        header.addStretch()
        header.addWidget(self.lbl_pos)
        
        # Map view - the main interaction area
        self.map_view = TileMapView(self.engine)
        self.map_view.setMinimumSize(600, 500)
        
        # Visualization options and zoom controls
        viz_layout = QHBoxLayout()
        
        # Zoom controls
        zoom_label = QLabel("Zoom:")
        viz_layout.addWidget(zoom_label)
        
        self.btn_zoom_in = QPushButton("🔍+")
        self.btn_zoom_in.setFixedWidth(50)
        self.btn_zoom_in.setToolTip("Zoom In")
        self.btn_zoom_in.clicked.connect(lambda: self.map_view.zoom_by_factor(1.25))
        
        self.btn_zoom_out = QPushButton("🔍-")
        self.btn_zoom_out.setFixedWidth(50)
        self.btn_zoom_out.setToolTip("Zoom Out")
        self.btn_zoom_out.clicked.connect(lambda: self.map_view.zoom_by_factor(0.8))
        
        self.btn_zoom_fit = QPushButton("⬜")
        self.btn_zoom_fit.setFixedWidth(50)
        self.btn_zoom_fit.setToolTip("Fit to View")
        self.btn_zoom_fit.clicked.connect(self.map_view.fit_to_view)
        
        viz_layout.addWidget(self.btn_zoom_in)
        viz_layout.addWidget(self.btn_zoom_out)
        viz_layout.addWidget(self.btn_zoom_fit)
        
        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFrameShadow(QFrame.Sunken)
        viz_layout.addWidget(separator)
        
        # Visualization options
        viz_layout.addWidget(QLabel("Display:"))
        
        self.chk_show_overlap = QCheckBox("Show Overlap")
        self.chk_show_overlap.setChecked(False)
        self.chk_show_overlap.stateChanged.connect(self._on_viz_changed)
        
        self.chk_show_mfov = QCheckBox("Show MFOV Grid")
        self.chk_show_mfov.setChecked(False)
        self.chk_show_mfov.stateChanged.connect(self._on_viz_changed)
        
        viz_layout.addWidget(self.chk_show_overlap)
        viz_layout.addWidget(self.chk_show_mfov)
        viz_layout.addStretch()
        
        # Region info display
        self.lbl_region_info = QLabel("No region selected")
        self.lbl_region_info.setStyleSheet("color: #4ec9b0; font-family: monospace; font-size: 11px;")
        viz_layout.addWidget(self.lbl_region_info)
        self.map_view.stage_moved.connect(self._on_map_clicked)
        self.map_view.image_dropped.connect(self._on_image_dropped)
        self.map_view.polygon_clicked.connect(self._on_polygon_clicked_on_map)
        
        # Bottom tabs for QC, Beam on Detector, and Logs
        bottom_tabs = QTabWidget()
        bottom_tabs.setMaximumHeight(250)
        
        # Beam on Detector Tab
        beam_widget = QWidget()
        beam_layout = QHBoxLayout(beam_widget)
        beam_layout.setContentsMargins(20, 20, 20, 20)
        
        # Beams to Fibers - simple layout
        btf_layout = QVBoxLayout()
        
        btf_title = QLabel("Beam Alignment (91 Beams)")
        btf_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        btf_layout.addWidget(btf_title)
        
        btf_desc = QLabel("Align electron beams to fiber positions.\nReduces crosstalk between sub-images.")
        btf_desc.setStyleSheet("color: #888; font-size: 11px;")
        btf_desc.setWordWrap(True)
        btf_layout.addWidget(btf_desc)
        
        btf_layout.addSpacing(10)
        
        self.btn_beams_to_fibers = QPushButton("🎯 Beams to Fibers")
        self.btn_beams_to_fibers.setToolTip("CMD_BEAMS_TO_FIBERS: Align beams to fiber positions")
        self.btn_beams_to_fibers.setStyleSheet("""
            QPushButton {
                background-color: #094771;
                font-weight: bold;
                padding: 12px 30px;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #0a5a8c; }
        """)
        self.btn_beams_to_fibers.clicked.connect(self._on_beams_to_fibers)
        btf_layout.addWidget(self.btn_beams_to_fibers)
        
        btf_layout.addSpacing(5)
        
        self.lbl_btf_status = QLabel("Status: Ready")
        self.lbl_btf_status.setStyleSheet("color: #888; font-size: 11px;")
        btf_layout.addWidget(self.lbl_btf_status)
        
        btf_layout.addStretch()
        beam_layout.addLayout(btf_layout)
        beam_layout.addStretch()
        
        bottom_tabs.addTab(beam_widget, "Beam on Detector")
        
        # QC Tab
        qc_widget = QWidget()
        qc_layout = QHBoxLayout(qc_widget)
        self.qc_panel = QCPanel()
        qc_layout.addWidget(self.qc_panel)
        bottom_tabs.addTab(qc_widget, "Quality Control")
        
        # Logs Tab
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        self.log_panel = LogPanel()
        log_layout.addWidget(self.log_panel)
        bottom_tabs.addTab(log_widget, "Logs")
        
        center_layout.addLayout(header)
        center_layout.addWidget(self.map_view, 1)
        center_layout.addLayout(viz_layout)
        center_layout.addWidget(bottom_tabs)
        
        main_split.addWidget(left_scroll)
        main_split.addWidget(center_widget)
        main_split.setSizes([320, 900])
        
        self.layout.addWidget(main_split)
        
        # Connect map signals
        self.map_view.roi_completed.connect(self._on_roi_completed)
        
        # DON'T override engine callbacks - use our own timer to poll instead
        # This avoids conflicting with main_window's status panel
        
        # Setup continuous position polling timer
        from PySide6.QtCore import QTimer
        self._position_timer = QTimer(self)
        self._position_timer.timeout.connect(self._poll_position)
        self._position_timer.start(500)  # Poll every 500ms
        
        # State
        self._current_image_path = None
        self._is_acquiring = False
        self._is_drawing = False
        self._last_position = (0.0, 0.0, 0.0)
        self._roi_list = []  # Store ROI coordinate lists
        self._support_points = {}  # Store support points per region: {region_idx: [(x, y), ...]}
        # Imaging JSON path (set via ParametersPanel.imaging_json_selected) — the
        # acquisition subprocess (focus_map.py) reads regions/contours from it.
        self._imaging_json_path = None
        # Subprocess driver for the real acquisition pipeline (lazy-created).
        self._pipeline_runner = None
        self._run_total_regions = 0
        self._run_current_region_idx = 0
        self._tile_items = {}  # Store tile graphics items: {(region_idx, tile_idx): QGraphicsEllipseItem}
        self._current_region_tiles = []  # Store tiles for current region during acquisition
        
    
    # === Event Handlers ===
    
    def _on_start_drawing(self):
        """Start drawing mode"""
        self.map_view.set_drawing_mode(True)
        self._is_drawing = True
        
        # Update UI
        self.btn_draw_polygon.setEnabled(False)
        self.btn_draw_polygon.setText("Drawing...")
        self.btn_load_roi.setEnabled(False)
        self.btn_apply_drawing.show()
        self.lbl_drawing_status.setText("Click on map to add points. Click near start to close polygon.")
        self.lbl_drawing_status.show()
        
        self.log_panel.append_log("ROI", "INFO", "Drawing mode started - click to add points")
    
    def _on_apply_drawing(self):
        """Apply and exit drawing mode"""
        self.map_view.set_drawing_mode(False)
        self._is_drawing = False
        
        # Update UI
        self.btn_draw_polygon.setEnabled(True)
        self.btn_draw_polygon.setText("Draw Polygon")
        self.btn_load_roi.setEnabled(True)
        self.btn_apply_drawing.hide()
        self.lbl_drawing_status.hide()
        
        self.log_panel.append_log("ROI", "INFO", f"Drawing mode ended - {len(self._roi_list)} regions defined")
    
    def _poll_position(self):
        """Poll stage position and operation status periodically"""
        if not self.engine:
            return
        
        try:
            # Get position from engine (reads from hardware if connected)
            pos = self.engine.get_position()
            if pos:
                x, y, z = pos.x, pos.y, pos.z if pos.z else 0
                # Only update display if position changed significantly (> 0.1 um)
                if abs(x - self._last_position[0]) > 0.1 or \
                   abs(y - self._last_position[1]) > 0.1 or \
                   abs(z - self._last_position[2]) > 0.1:
                    self._last_position = (x, y, z)
                    self._update_position_display(x, y, z)
            
            # Update operation status from engine
            if hasattr(self, 'lbl_operation') and hasattr(self.engine, '_current_operation'):
                op = self.engine._current_operation
                if op == 'idle':
                    self.lbl_operation.setText("Status: Idle")
                    self.lbl_operation.setStyleSheet("font-family: monospace; font-size: 10px; color: #888;")
                elif op == 'moving':
                    self.lbl_operation.setText("Status: Moving...")
                    self.lbl_operation.setStyleSheet("font-family: monospace; font-size: 10px; color: #4ec9b0;")
                else:
                    self.lbl_operation.setText(f"Status: {op}")
                    self.lbl_operation.setStyleSheet("font-family: monospace; font-size: 10px; color: #dcdcaa;")
        except Exception as e:
            pass  # Silently ignore polling errors
    
    
    def _update_position_display(self, x: float, y: float, z: float):
        """Update all position-related displays"""
        # Update position label
        if hasattr(self, 'lbl_stage_pos'):
            self.lbl_stage_pos.setText(f"Stage: X={x:.1f} Y={y:.1f} Z={z:.1f} µm")
        
        # Update crosshair on map to follow stage movement
        self.map_view.draw_crosshair_um(x, y)
    
    def _move_stage_relative(self, dx: float, dy: float):
        """Move stage by relative amount"""
        if not self.engine:
            self.log_panel.append_log("Stage", "ERROR", "No engine connected")
            return
        
        try:
            # Read current position from hardware for accuracy
            pos = self.engine.get_position()
            if pos:
                new_x = pos.x + dx
                new_y = pos.y + dy
                self.log_panel.append_log("Stage", "INFO", f"Current: ({pos.x:.1f}, {pos.y:.1f}) -> Moving by ({dx:.1f}, {dy:.1f})")
                self.engine.move_stage(new_x, new_y)
        except Exception as e:
            self.log_panel.append_log("Stage", "ERROR", f"Move failed: {e}")
    
    def _on_read_position(self):
        """Read current position from hardware"""
        if not self.engine:
            self.log_panel.append_log("Stage", "ERROR", "No engine connected")
            return
        
        try:
            pos = self.engine.get_position()
            if pos:
                self._update_position_display(pos.x, pos.y, pos.z if pos.z else 0)
                # Also update Go To fields with current position
                self.spin_goto_x.setValue(pos.x)
                self.spin_goto_y.setValue(pos.y)
                self.log_panel.append_log("Stage", "INFO", f"Position: X={pos.x:.1f}, Y={pos.y:.1f}, Z={pos.z:.1f if pos.z else 0:.1f}")
        except Exception as e:
            self.log_panel.append_log("Stage", "ERROR", f"Read failed: {e}")
    
    # === Beam on Detector (Scintillator) ===
    
    def _on_beams_to_fibers(self):
        """Execute CMD_BEAMS_TO_FIBERS to align beams to fiber positions"""
        if not self.engine:
            self.lbl_btf_status.setText("Status: Not connected")
            self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 10px;")
            return
        
        try:
            self.lbl_btf_status.setText("Status: Aligning beams...")
            self.lbl_btf_status.setStyleSheet("color: #4ec9b0; font-size: 11px;")
            self.btn_beams_to_fibers.setEnabled(False)
            QApplication.processEvents()
            
            # Execute CMD_BEAMS_TO_FIBERS
            if hasattr(self.engine, '_conn') and self.engine._conn:
                print("Executing CMD_BEAMS_TO_FIBERS...")
                ret = self.engine._conn.execute("CMD_BEAMS_TO_FIBERS")
                print(f"CMD_BEAMS_TO_FIBERS returned: {ret}")
                
                if ret == 0:
                    self.lbl_btf_status.setText("Status: ✓ Beams aligned")
                    self.lbl_btf_status.setStyleSheet("color: #4ec9b0; font-size: 11px;")
                    self.log_panel.append_log("Beam", "INFO", "Beams to Fibers completed")
                elif ret == -1:
                    self.lbl_btf_status.setText("Status: ✗ No beams visible")
                    self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 11px;")
                    self.log_panel.append_log("Beam", "ERROR", "No beams visible")
                elif ret == -2:
                    self.lbl_btf_status.setText("Status: ✗ Beams blanked")
                    self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 11px;")
                    self.log_panel.append_log("Beam", "ERROR", "Beams are blanked")
                else:
                    self.lbl_btf_status.setText(f"Status: ✗ Failed ({ret})")
                    self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 11px;")
                    self.log_panel.append_log("Beam", "ERROR", f"Failed with code {ret}")
            else:
                print("No engine connection available")
                self.lbl_btf_status.setText("Status: No connection")
                self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 11px;")
                
        except Exception as e:
            error_msg = str(e)
            print(f"Beams to Fibers error: {error_msg}")
            import traceback
            traceback.print_exc()
            self.lbl_btf_status.setText(f"Status: Error - {error_msg[:30]}")
            self.lbl_btf_status.setStyleSheet("color: #f44747; font-size: 10px;")
            self.log_panel.append_log("Beam", "ERROR", error_msg)
        finally:
            self.btn_beams_to_fibers.setEnabled(True)
    
    def _move_stage_to_crosshair(self):
        """Move stage to the current crosshair position on map"""
        if not self.engine:
            self.log_panel.append_log("Stage", "ERROR", "No engine connected")
            return
        
        # Get crosshair position from map (if any)
        if hasattr(self.map_view, 'crosshair_items') and self.map_view.crosshair_items:
            # Crosshair is drawn at pixel coords, convert to um
            # For now, use the center (0, 0)
            self.log_panel.append_log("Stage", "INFO", "Moving to map center (0, 0)")
            self.lbl_operation.setText("Status: Moving...")
            self.engine.move_stage(0, 0)
    
    def _on_goto_position(self):
        """Move stage to specified X, Y coordinates"""
        if not self.engine:
            self.log_panel.append_log("Stage", "ERROR", "No engine connected")
            return
        
        x = self.spin_goto_x.value()
        y = self.spin_goto_y.value()
        self.lbl_operation.setText("Status: Moving...")
        self.engine.move_stage(x, y)
        self.log_panel.append_log("Stage", "INFO", f"Moving to ({x:.1f}, {y:.1f})")
    
    def _on_polygon_clicked_on_map(self, region_idx: int):
        """Handle polygon click on map - select corresponding item in list"""
        if region_idx < self.regions_list.count():
            # Select the item in the list (this will trigger _on_region_selected)
            self.regions_list.setCurrentRow(region_idx)
            print(f"[AcquisitionPage] Selected Region {region_idx + 1} from map click")
    
    def _on_region_selected(self, item):
        """Handle region selection in list - highlight on map and show info"""
        idx = self.regions_list.row(item)
        
        # Highlight the selected region on map
        self.map_view.highlight_roi(idx)
        
        # Refresh MFOV grid if checkbox is enabled
        if self.chk_show_mfov.isChecked():
            self.map_view.set_visualization_options(
                self.chk_show_overlap.isChecked(), 
                True
            )
        
        # Calculate and display region info
        if idx < len(self._roi_list):
            points = self._roi_list[idx]
            
            # Calculate center
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)
            
            # Calculate tiles count using API
            tiles_count = self._calculate_tiles_for_region(points)
            
            # Update info label
            info_text = f"Region {idx + 1}: {len(points)} vertices | Center: ({center_x:.3f}, {center_y:.3f}) | Tiles: {tiles_count}"
            self.lbl_region_info.setText(info_text)
            
            # Update support points status
            sp_count = len(self._support_points.get(idx, []))
            self.lbl_support_points_status.setText(f"Region {idx+1}: {sp_count} support point(s)")
            
            # Update support points table
            self._update_support_points_table(idx)
            
            self.log_panel.append_log("ROI", "INFO", f"Selected {info_text}")
    
    def _on_delete_region(self):
        """Delete selected region"""
        current = self.regions_list.currentItem()
        if current:
            idx = self.regions_list.row(current)
            self.regions_list.takeItem(idx)
            
            # Remove from map and list
            if idx < len(self._roi_list):
                self._roi_list.pop(idx)
            
            # Remove polygon from map
            if idx < len(self.map_view._roi_polygons):
                poly = self.map_view._roi_polygons.pop(idx)
                self.map_view.scene.removeItem(poly)
            
            self._update_roi_count()
            self.log_panel.append_log("ROI", "INFO", f"Deleted Region {idx + 1}")
    
    def _on_load_roi(self):
        """Load ROI from CSV or JSON file"""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Load ROI File",
            "",
            "ROI Files (*.csv *.json);;CSV Files (*.csv);;JSON Files (*.json);;All Files (*)"
        )
        if filepath:
            self._load_roi_file(filepath)
    
    def _load_roi_file(self, filepath: str):
        """Load ROI coordinates from file"""
        import os
        import json
        
        ext = os.path.splitext(filepath)[1].lower()
        
        try:
            if ext == '.csv':
                self._load_roi_csv(filepath)
            elif ext == '.json':
                self._load_roi_json(filepath)
            else:
                QMessageBox.warning(self, "Error", f"Unsupported file type: {ext}")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to load ROI: {e}")
            import traceback
            traceback.print_exc()
    
    def _load_roi_csv(self, filepath: str):
        """Load ROI from CSV file (aligned_polygons.csv format)"""
        import csv
        
        polygons = {}  # polygon_id -> list of (x, y)
        
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try different column name formats
                poly_id = row.get('polygon_id', row.get('id', row.get('roi_id', '0')))
                
                # Get coordinates
                x = float(row.get('x', row.get('X', row.get('col', 0))))
                y = float(row.get('y', row.get('Y', row.get('row', 0))))
                
                if poly_id not in polygons:
                    polygons[poly_id] = []
                polygons[poly_id].append((x, y))
        
        # Add each polygon as ROI
        colors = [
            QColor(255, 100, 0),   # Orange
            QColor(0, 200, 100),   # Green
            QColor(100, 150, 255), # Blue
            QColor(255, 50, 150),  # Pink
            QColor(200, 200, 0),   # Yellow
        ]
        
        for i, (poly_id, points) in enumerate(polygons.items()):
            if len(points) >= 3:
                color = colors[i % len(colors)]
                self.map_view.add_roi_from_points(points, color)
                self._roi_list.append(points)
                
                # Calculate tiles and add to regions list
                region_num = len(self._roi_list)
                tiles_count = self._calculate_tiles_for_region(points)
                item = QListWidgetItem(f"Region {region_num}: {len(points)}v, {tiles_count} tiles")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)  # Enabled by default
                self.regions_list.addItem(item)
        
        self._update_roi_count()
        self.log_panel.append_log("ROI", "INFO", f"Loaded {len(polygons)} regions from CSV")
    
    def _load_roi_json(self, filepath: str):
        """Load ROI from JSON file"""
        import json
        
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        # Handle different JSON formats
        rois = []
        
        if 'Regions' in data:
            # ZEISS format
            for region in data['Regions']:
                if 'Points' in region:
                    points = [(p['X'], p['Y']) for p in region['Points']]
                    rois.append(points)
        elif 'rois' in data:
            # Simple format
            rois = data['rois']
        elif isinstance(data, list):
            # List of polygons
            rois = data
        
        colors = [
            QColor(255, 100, 0),
            QColor(0, 200, 100),
            QColor(100, 150, 255),
            QColor(255, 50, 150),
        ]
        
        for i, points in enumerate(rois):
            if len(points) >= 3:
                # Convert to tuples if needed
                if isinstance(points[0], dict):
                    points = [(p['x'], p['y']) for p in points]
                color = colors[i % len(colors)]
                self.map_view.add_roi_from_points(points, color)
                self._roi_list.append(points)
                
                # Calculate tiles and add to regions list
                region_num = len(self._roi_list)
                tiles_count = self._calculate_tiles_for_region(points)
                item = QListWidgetItem(f"Region {region_num}: {len(points)}v, {tiles_count} tiles")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)  # Enabled by default
                self.regions_list.addItem(item)
        
        self._update_roi_count()
        self.log_panel.append_log("ROI", "INFO", f"Loaded {len(rois)} regions from JSON")
    
    def _on_roi_completed(self, points: list):
        """Handle completed ROI polygon from drawing"""
        self._roi_list.append(points)
        
        # Calculate info for this region
        region_num = len(self._roi_list)
        tiles_count = self._calculate_tiles_for_region(points)
        
        # Add to regions list with detailed info and checkbox
        item = QListWidgetItem(f"Region {region_num}: {len(points)}v, {tiles_count} tiles")
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)  # Enabled by default
        self.regions_list.addItem(item)
        
        self._update_roi_count()
        self.log_panel.append_log("ROI", "INFO", f"Region {region_num} created with {len(points)} vertices, {tiles_count} tiles")
        
        # Update drawing status - stay in drawing mode for more regions
        if self._is_drawing:
            self.lbl_drawing_status.setText(f"{region_num} region(s) defined. Continue drawing or click Apply.")
    
    def _on_clear_rois(self):
        """Clear all ROIs"""
        self.map_view.clear_rois()
        self._roi_list.clear()
        self.regions_list.clear()
        self._support_points.clear()  # Clear support points too
        self.map_view.clear_support_points()
        self._update_roi_count()
        self.lbl_support_points_status.setText("No region selected")
        self.log_panel.append_log("ROI", "INFO", "All regions cleared")
    
    def _on_apply_support_points(self):
        """Apply support points to selected region"""
        current = self.regions_list.currentItem()
        if not current:
            QMessageBox.warning(self, "No Selection", "Please select a region first")
            return
        
        idx = self.regions_list.row(current)
        if idx >= len(self._roi_list):
            return
        
        # Get region polygon (in pixel coordinates)
        points = self._roi_list[idx]
        count = self.spin_support_points.value()
        
        print(f"[AcquisitionPage] Applying {count} support points to Region {idx+1}")
        print(f"  Region bounds: {min(p[0] for p in points):.1f} to {max(p[0] for p in points):.1f} (x)")
        print(f"                 {min(p[1] for p in points):.1f} to {max(p[1] for p in points):.1f} (y)")
        
        # Distribute support points within polygon (returns pixel coordinates)
        support_pts = self._distribute_support_points_in_polygon(points, count)
        
        print(f"  Generated {len(support_pts)} support points")
        if support_pts:
            print(f"  First point: ({support_pts[0][0]:.1f}, {support_pts[0][1]:.1f})")
        
        # Store support points (pixel coordinates)
        self._support_points[idx] = support_pts
        
        # Draw support points on map (specify pixel coordinates)
        self.map_view.draw_support_points(idx, support_pts, coordinates='pixel')
        
        # Update status
        self.lbl_support_points_status.setText(f"Region {idx+1}: {len(support_pts)} support point(s)")
        self.log_panel.append_log("Support Points", "SUCCESS", 
                                  f"Added {len(support_pts)} support points to Region {idx+1}")
        
        # Update table
        self._update_support_points_table(idx)
    
    def _on_clear_support_points(self):
        """Clear support points from selected region"""
        current = self.regions_list.currentItem()
        if not current:
            # Clear all support points
            self._support_points.clear()
            self.map_view.clear_support_points()
            self.lbl_support_points_status.setText("All support points cleared")
            self.log_panel.append_log("Support Points", "INFO", "Cleared all support points")
            self._update_support_points_table(None)  # Clear table
        else:
            # Clear support points for selected region
            idx = self.regions_list.row(current)
            if idx in self._support_points:
                del self._support_points[idx]
            self.map_view.clear_support_points(idx)
            self.lbl_support_points_status.setText(f"Region {idx+1}: 0 support point(s)")
            self.log_panel.append_log("Support Points", "INFO", f"Cleared support points from Region {idx+1}")
            self._update_support_points_table(idx)  # Update table for selected region
    
    def _update_support_points_table(self, region_idx):
        """Update the support points coordinate table for the selected region
        
        Args:
            region_idx: Index of the region to display support points for (None to clear)
        """
        # Clear table
        self.support_points_table.setRowCount(0)
        
        if region_idx is None or region_idx not in self._support_points:
            return
        
        # Get support points for this region (in pixel coordinates)
        support_pts_pixel = self._support_points[region_idx]
        
        if not support_pts_pixel:
            return
        
        # Set row count
        self.support_points_table.setRowCount(len(support_pts_pixel))
        
        # Populate table with coordinates
        for i, (px, py) in enumerate(support_pts_pixel):
            # Convert pixel to um coordinates
            x_um, y_um = self.map_view.pixel_to_um(px, py)
            
            # Point number
            item_num = QTableWidgetItem(f"Point {i+1}")
            item_num.setTextAlignment(Qt.AlignCenter)
            self.support_points_table.setItem(i, 0, item_num)
            
            # X coordinate
            item_x = QTableWidgetItem(f"{x_um:.3f}")
            item_x.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.support_points_table.setItem(i, 1, item_x)
            
            # Y coordinate
            item_y = QTableWidgetItem(f"{y_um:.3f}")
            item_y.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.support_points_table.setItem(i, 2, item_y)
        
        # Resize columns to content
        self.support_points_table.resizeColumnsToContents()
    
    def _distribute_support_points_in_polygon(self, polygon_points, count):
        """Distribute points uniformly within a polygon using grid sampling"""
        import numpy as np
        try:
            from shapely.geometry import Polygon, Point
        except ImportError:
            self.log_panel.append_log("Support Points", "ERROR", 
                                      "shapely library required. Install with: pip install shapely")
            # Fallback: return points along polygon boundary
            n = len(polygon_points) - 1
            indices = np.linspace(0, n-1, count, dtype=int)
            return [polygon_points[i] for i in indices]
        
        # Create shapely polygon (remove last duplicate point)
        poly = Polygon(polygon_points[:-1])
        
        # Get bounding box
        minx, miny, maxx, maxy = poly.bounds
        
        # Grid sampling with padding
        padding = (maxx - minx) * 0.05  # 5% padding
        grid_size = int(np.ceil(np.sqrt(count * 4)))  # Oversample
        x_coords = np.linspace(minx + padding, maxx - padding, grid_size)
        y_coords = np.linspace(miny + padding, maxy - padding, grid_size)
        
        # Find all points inside polygon
        inside_points = []
        for x in x_coords:
            for y in y_coords:
                if poly.contains(Point(x, y)):
                    inside_points.append((float(x), float(y)))
        
        # If we have enough points, select evenly distributed ones
        if len(inside_points) >= count:
            # Simple even spacing
            step = len(inside_points) // count
            selected_points = inside_points[::step][:count]
        else:
            # Not enough points, use what we have
            selected_points = inside_points
            if len(selected_points) < count:
                self.log_panel.append_log("Support Points", "WARNING", 
                                          f"Only {len(selected_points)} points fit in region (requested {count})")
        
        return selected_points
    
    def _update_roi_count(self):
        """Update ROI count and tile count"""
        count = self.map_view.get_roi_count()
        
        # Calculate total tiles for all regions
        total_tiles = 0
        for points in self._roi_list:
            total_tiles += self._calculate_tiles_for_region(points)
        
        self.lbl_tile_count.setText(str(total_tiles))
    
    def _get_travel_direction(self):
        """Get TravelDirection enum from UI combo box"""
        from msem.roi_acquisition import TravelDirection
        
        direction_text = self.combo_direction.currentText().lower().replace(" ", "_")
        print(f"[DEBUG] combo_direction text: '{self.combo_direction.currentText()}' -> '{direction_text}'")
        direction_map = {
            'horizontal_up': TravelDirection.HORIZONTAL_UP,
            'horizontal_down': TravelDirection.HORIZONTAL_DOWN,
            'vertical_left': TravelDirection.VERTICAL_LEFT,
            'vertical_right': TravelDirection.VERTICAL_RIGHT,
        }
        result = direction_map.get(direction_text, TravelDirection.HORIZONTAL_UP)
        print(f"[DEBUG] _get_travel_direction() -> {result}")
        return result
    
    def _get_start_position(self):
        """Get StartPosition enum from UI combo box"""
        from msem.roi_acquisition import StartPosition
        
        start_text = self.combo_start.currentText().lower().replace(" ", "_")
        start_map = {
            'bottom_left': StartPosition.BOTTOM_LEFT,
            'bottom_right': StartPosition.BOTTOM_RIGHT,
            'top_left': StartPosition.TOP_LEFT,
            'top_right': StartPosition.TOP_RIGHT,
        }
        return start_map.get(start_text, StartPosition.BOTTOM_LEFT)
    
    def _get_travel_pattern(self):
        """Get TravelPattern enum from UI combo box"""
        from msem.roi_acquisition import TravelPattern
        
        pattern_text = self.combo_travel.currentText().lower()
        if 'comb' in pattern_text:
            return TravelPattern.COMB
        elif 'serp' in pattern_text or 'snake' in pattern_text:
            return TravelPattern.SNAKE
        return TravelPattern.COMB
    
    def _calculate_tiles_for_region(self, points: list) -> int:
        """Calculate number of tiles (MFOV) for a region using MultiSEM API"""
        if len(points) < 3:
            return 0
        
        try:
            # Check if we have a connection to the microscope
            if not hasattr(self, 'engine') or not self.engine:
                return self._estimate_tiles_geometric(points)
            
            # Check if engine is connected
            if not self.engine.is_connected():
                return self._estimate_tiles_geometric(points)
            
            # Use ROI API to calculate exact tiles count
            try:
                from msem.roi_acquisition import ROIAcquisition, ROIConfig, TravelPattern, TravelDirection, StartPosition
            except ImportError:
                return self._estimate_tiles_geometric(points)
            
            # Get the connection from engine
            conn = self.engine.get_msem_connection()
            if not conn:
                return self._estimate_tiles_geometric(points)
            
            # Create ROI acquisition object
            roi_acq = ROIAcquisition(conn)
            
            # Get parameters from UI
            overlap_pct = self.spin_overlap_mfov.value() / 100.0  # Convert to 0-1 range
            pixelsize = self.spin_pixelsize.value()
            
            # Set configuration with UI-selected travel parameters
            config = ROIConfig(
                outer_mfov_overlap=overlap_pct,
                inner_sfov_overlap=self.spin_overlap_sfov.value(),
                pixelsize=pixelsize,
                travel_pattern=self._get_travel_pattern(),
                travel_direction=self._get_travel_direction(),
                start_position=self._get_start_position(),
            )
            roi_acq.set_config(config)
            
            # IMPORTANT: Convert pixel coordinates to µm for API
            # points are in pixel coordinates, API needs µm
            points_um = [self.map_view.pixel_to_um(px, py) for px, py in points]
            roi_acq.define_roi(points_um)
            
            # Calculate tiles using API
            tiles = roi_acq.calculate_tiles()
            
            return len(tiles)
            
        except Exception as e:
            print(f"API calculation failed, using geometric estimation: {e}")
            import traceback
            traceback.print_exc()
            return self._estimate_tiles_geometric(points)
    
    def _estimate_tiles_geometric(self, points: list) -> int:
        """Geometric estimation of tiles when API is not available
        
        Note: points are in PIXEL coordinates, need to convert to µm for calculation
        """
        if len(points) < 3:
            return 0
        
        try:
            # Get overlap from UI (default to 4% if not available)
            try:
                overlap_pct = self.spin_overlap_mfov.value()
            except:
                overlap_pct = 4.0
            
            # MFOV diameter for 91-beam MSEM (typical: ~110 µm)
            mfov_diameter = 110.0  # µm
            
            # Calculate effective step size considering overlap
            step_size = mfov_diameter * (1 - overlap_pct / 100.0)
            
            # Get pixel size for conversion (default to 1.0 if not available)
            pixel_size_um = getattr(self.map_view, 'pixel_size_um', 1.0)
            
            # Calculate bounding box area in PIXELS first
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            width_px = max(xs) - min(xs)
            height_px = max(ys) - min(ys)
            
            # Convert to µm
            width_um = width_px * pixel_size_um
            height_um = height_px * pixel_size_um
            
            # Hexagonal grid calculation
            # Horizontal spacing: step_size
            # Vertical spacing: step_size * sqrt(3)/2
            rows = int(height_um / (step_size * 0.866) + 1)  # sqrt(3)/2 ≈ 0.866
            cols = int(width_um / step_size + 1)
            
            # Hexagonal packing: alternating rows have offset
            # Approximate: average of rows with cols and cols-1
            approx_tiles = (rows * cols + rows * (cols - 1)) // 2
            
            # Add some margin for boundary (5-10%)
            approx_tiles = int(approx_tiles * 1.08)
            
            return max(1, approx_tiles)
            
        except Exception as e:
            print(f"Error in geometric estimation: {e}")
            return 0
    
    def _on_viz_changed(self):
        """Handle visualization option changes"""
        show_overlap = self.chk_show_overlap.isChecked()
        show_mfov = self.chk_show_mfov.isChecked()
        
        # Update map view visualization
        self.map_view.set_visualization_options(show_overlap, show_mfov)
        
        self.log_panel.append_log("UI", "INFO", 
            f"Visualization: Overlap={show_overlap}, MFOV={show_mfov}")
    
    def _on_param_changed(self):
        """Handle parameter changes - recalculate tiles and update display"""
        # Recalculate tiles for all regions
        self._update_roi_count()
        
        # Update region info if one is selected
        current_item = self.regions_list.currentItem()
        if current_item:
            self._on_region_selected(current_item)
        
        # Log parameter change
        self.log_panel.append_log("Config", "INFO", 
            f"Parameters updated: MFOV Overlap={self.spin_overlap_mfov.value()}%, "
            f"Pixel Size={self.spin_pixelsize.value()}nm")
    
    def update_from_params_panel(self, params: dict):
        """Update left panel controls from Parameters Panel (right side)"""
        print(f"[Acquisition Page] Received parameters_updated signal with {len(params)} parameters")
        print(f"  Keys: {list(params.keys())}")
        
        # Use blockSignals to prevent triggering valueChanged during batch update
        widgets_to_block = [
            self.spin_overlap_mfov,
            self.spin_overlap_sfov,
            self.spin_pixelsize,
            self.combo_scanrate,
            self.combo_travel,
            self.combo_direction,
            self.combo_start,
            self.edit_output_path,
            self.edit_prefix,
            self.combo_format
        ]
        
        for widget in widgets_to_block:
            widget.blockSignals(True)
        
        # Update Scan Parameters
        if 'pixel_size' in params:
            print(f"  Setting pixel_size: {params['pixel_size']} nm")
            self.spin_pixelsize.setValue(params['pixel_size'])
        if 'scan_rate' in params:
            print(f"  Setting scan_rate: {params['scan_rate']}")
            # Find matching scan rate in combo
            for i in range(self.combo_scanrate.count()):
                if self.combo_scanrate.itemData(i) == params['scan_rate']:
                    self.combo_scanrate.setCurrentIndex(i)
                    print(f"    Found at index {i}")
                    break
        
        # Update ROI Settings
        if 'mfov_overlap' in params:
            print(f"  Setting mfov_overlap: {params['mfov_overlap']} %")
            self.spin_overlap_mfov.setValue(params['mfov_overlap'])
        if 'sfov_overlap' in params:
            print(f"  Setting sfov_overlap: {params['sfov_overlap']} µm")
            self.spin_overlap_sfov.setValue(params['sfov_overlap'])
        if 'travel_pattern' in params:
            pattern_map = {'comb': 0, 'snake': 1, 'serpentine': 1, 'spiral': 2}
            idx = pattern_map.get(params['travel_pattern'].lower(), 0)
            print(f"  Setting travel_pattern: '{params['travel_pattern']}' -> index {idx}")
            self.combo_travel.setCurrentIndex(idx)
        if 'travel_direction' in params:
            dir_map = {
                'horizontal up': 0,
                'horizontal_up': 0,
                'horizontal down': 1,
                'horizontal_down': 1,
                'vertical left': 2,
                'vertical_left': 2,
                'vertical right': 3,
                'vertical_right': 3
            }
            idx = dir_map.get(params['travel_direction'].lower().replace(' ', '_'), 0)
            print(f"  Setting travel_direction: '{params['travel_direction']}' -> index {idx}")
            self.combo_direction.setCurrentIndex(idx)
        
        # Update Output Settings
        if 'output_folder' in params:
            print(f"  Setting output_folder: {params['output_folder']}")
            self.edit_output_path.setText(params['output_folder'])
        if 'output_prefix' in params:
            print(f"  Setting output_prefix: {params['output_prefix']}")
            self.edit_prefix.setText(params['output_prefix'])
        if 'output_format' in params:
            idx = self.combo_format.findText(params['output_format'])
            if idx >= 0:
                print(f"  Setting output_format: {params['output_format']} -> index {idx}")
                self.combo_format.setCurrentIndex(idx)
            else:
                print(f"  WARNING: Format '{params['output_format']}' not found in combo")
        
        # Unblock signals
        for widget in widgets_to_block:
            widget.blockSignals(False)
        
        # Trigger recalculation once at the end
        self._on_param_changed()
        
        print(f"[Acquisition Page] All parameters updated successfully")
        self.log_panel.append_log("Parameters", "SUCCESS", 
            f"✓ Updated: Pixel={params.get('pixel_size')}nm, MFOV={params.get('mfov_overlap')}%, "
            f"Output={params.get('output_folder')}")
    
    def update_stage_position(self, x: float, y: float, z: float):
        """Update realtime crosshair position on map when stage moves"""
        self.map_view.update_realtime_crosshair(x, y)
    
    def _on_offset_changed(self):
        """Handle manual offset adjustment"""
        offset_x = self.spin_offset_x.value()
        offset_y = self.spin_offset_y.value()
        self.tile_map.set_manual_offset(offset_x, offset_y)
        # Trigger redraw of ROIs with new offset
        self._redraw_all_rois()
    
    def _redraw_all_rois(self):
        """Redraw all ROIs with current offset"""
        if hasattr(self, '_loaded_regions') and self._loaded_regions:
            # Clear existing ROI items from map_view
            self.map_view.clear_rois()
            self._roi_list.clear()
            
            # Redraw all regions
            for idx, region in enumerate(self._loaded_regions):
                if 'polygon_points' in region:
                    points_um = region['polygon_points']
                    # Convert um to pixels using the new offset
                    points_px = [self.map_view.um_to_pixel(x, y) for x, y in points_um]
                    
                    # Store and draw
                    self._roi_list.append(points_px)
                    self.map_view.add_polygon_roi(points_px)
                    
                    # Also redraw support points if available
                    if 'support_points' in region and region['support_points']:
                        sp_px = []
                        for sp in region['support_points']:
                            px, py = self.map_view.um_to_pixel(sp['x'], sp['y'])
                            sp_px.append((px, py))
                        self.map_view.draw_support_points(idx, sp_px, coordinates='pixel')
    
    def _on_lock_toggle(self):
        """Toggle lock state for mask editing"""
        is_locked = self.btn_lock_masks.isChecked()
        if is_locked:
            self.btn_lock_masks.setText("🔒 Locked")
            self.map_view.set_masks_editable(False)
            self.log_panel.append_log("ROI", "INFO", "Masks locked - editing disabled")
        else:
            self.btn_lock_masks.setText("🔓 Unlocked")
            self.map_view.set_masks_editable(True)
            self.log_panel.append_log("ROI", "INFO", "Masks unlocked - you can now move/rotate regions")
    
    def _on_select_all_regions(self):
        """Enable all regions"""
        for i in range(self.regions_list.count()):
            item = self.regions_list.item(i)
            item.setCheckState(Qt.Checked)
    
    def _on_deselect_all_regions(self):
        """Disable all regions"""
        for i in range(self.regions_list.count()):
            item = self.regions_list.item(i)
            item.setCheckState(Qt.Unchecked)
    
    def _on_region_checkbox_changed(self, item):
        """Handle checkbox state change for a region"""
        row = self.regions_list.row(item)
        is_enabled = item.checkState() == Qt.Checked
        
        # Update region visibility on map
        self._update_region_visibility(row, is_enabled)
        
        # Update enabled count
        enabled_count = sum(1 for i in range(self.regions_list.count()) 
                          if self.regions_list.item(i).checkState() == Qt.Checked)
        total_count = self.regions_list.count()
        self.log_panel.append_log("ROI", "INFO", f"Region {row + 1} {'enabled' if is_enabled else 'disabled'} ({enabled_count}/{total_count} active)")
    
    def _update_region_visibility(self, region_idx: int, visible: bool):
        """Update visibility of a region on the map"""
        # Get ROI items from map_view
        if hasattr(self.map_view, 'roi_items') and region_idx < len(self.map_view.roi_items):
            item = self.map_view.roi_items[region_idx]
            item.setVisible(visible)
        
        # Also update support points visibility
        if region_idx in self.map_view.support_point_items:
            for sp_item in self.map_view.support_point_items[region_idx]:
                sp_item.setVisible(visible)
    
    def get_enabled_regions(self):
        """Get list of enabled region indices (for acquisition)"""
        enabled = []
        for i in range(self.regions_list.count()):
            if self.regions_list.item(i).checkState() == Qt.Checked:
                enabled.append(i)
        return enabled
    
    def _on_load_image(self):
        """Open file dialog to load image"""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Load Optical Image",
            "",
            "Images (*.czi *.png *.jpg *.jpeg *.bmp *.tif *.tiff);;All Files (*)"
        )
        if filepath:
            self._load_image(filepath)
    
    def _on_image_dropped(self, filepath: str):
        """Handle image dropped onto map"""
        self._load_image(filepath)
    
    def _load_image(self, filepath: str):
        """Load image into map view"""
        import os
        
        if self.map_view.load_image_with_progress(filepath):
            self._current_image_path = filepath
            
            # Update info with size
            size_mb = os.path.getsize(filepath) / 1024 / 1024
            filename = os.path.basename(filepath)
            
            # Get image dimensions
            if self.map_view.pixmap_item:
                w = self.map_view.pixmap_item.pixmap().width()
                h = self.map_view.pixmap_item.pixmap().height()
                self.lbl_image_info.setText(f"{filename}\n{w}x{h} px | {size_mb:.1f} MB | Original size")
            else:
                self.lbl_image_info.setText(f"{filename} ({size_mb:.1f} MB)")
            
            self.lbl_image_info.setStyleSheet("color: #4ec9b0;")
            
            self.log_panel.append_log("Setup", "INFO", f"Loaded: {filename} at full resolution")
    
    def _on_clear_map(self):
        """Clear the map"""
        self.map_view.clear_map()
        self._current_image_path = None
        self.lbl_image_info.setText("No image loaded")
        self.lbl_image_info.setStyleSheet("color: #666; font-style: italic;")
        self.log_panel.append_log("Setup", "INFO", "Map cleared")
    
    def _on_map_clicked(self, x: float, y: float):
        """Handle click on map - move stage"""
        self.lbl_pos.setText(f"Target: X={x:.3f}, Y={y:.3f}")
        self.log_panel.append_log("Stage", "INFO", f"Moving to ({x:.3f}, {y:.3f})")
        
        # Call engine to move stage
        if self.engine.is_connected():
            self.engine.move_stage(x, y)
    
    def _browse_output(self):
        """Browse for output directory"""
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if folder:
            self.edit_output_path.setText(folder)

    def _browse_prev_focusmap_dir(self):
        """Browse for the folder of per-section prev_focusmap *.txt files."""
        folder = QFileDialog.getExistingDirectory(self, "Select Prev Focus-map Directory")
        if folder:
            self.edit_prev_focusmap_dir.setText(folder)

    def _on_thumbnail_toggle(self, state):
        """Enable/disable thumbnail settings based on checkbox"""
        enabled = state == Qt.Checked
        self.edit_thumb_prefix.setEnabled(enabled)
        self.combo_thumb_format.setEnabled(enabled)
        self.spin_thumb_quality.setEnabled(enabled)
    
    def _on_start_acquisition(self):
        """Start acquisition by launching the real pipeline as a subprocess."""
        # Check prerequisites — the pipeline reads regions/contours from the
        # imaging JSON, so that's the hard requirement (not the optical image).
        if not self._imaging_json_path:
            QMessageBox.warning(
                self, "No imaging JSON",
                "Load a Zeiss experiment JSON first (Parameters panel → Load Config).\n"
                "The acquisition pipeline reads its regions, contours and support "
                "points from that file.")
            return

        if len(self._roi_list) == 0:
            QMessageBox.warning(self, "No regions", "No regions loaded. Load the imaging JSON's regions first.")
            return
        
        # Check if connected to hardware
        if not self.engine.is_connected():
            QMessageBox.warning(self, "Not Connected", 
                              "Please connect to MultiSEM hardware first (disable Simulation mode)")
            return
        
        # Generate output folder structure
        output_structure = self._generate_output_structure()
        
        # Calculate total tiles
        total_tiles = sum(self._calculate_tiles_for_region(points) for points in self._roi_list)
        
        # Check support points
        regions_with_sp = [i for i in range(len(self._roi_list)) if i in self._support_points]
        sp_info = f"{len(regions_with_sp)} region(s) with support points" if regions_with_sp else "No support points defined"
        
        # Show confirmation dialog with structure
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Start Acquisition")
        msg.setText(f"Ready to acquire {len(self._roi_list)} region(s), {total_tiles} tiles")
        msg.setInformativeText(
            f"<b>Output Structure:</b><br>"
            f"<pre>{output_structure['block_folder']}/\n"
            f"  ├── Region_1/ (Tile folders: 0001, 0002, ...)\n"
            f"  ├── Region_2/ ...\n"
            f"  └── Region_{len(self._roi_list)}/</pre><br>"
            f"<small>Each tile folder contains 91 images (91-beam MSEM)</small><br><br>"
            f"<b>Support Points:</b> {sp_info}<br>"
            f"<small>Pre-acquisition autofocus will verify ≥80% pass rate</small>"
        )
        msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        
        if msg.exec() != QMessageBox.Ok:
            self.log_panel.append_log("Acquisition", "INFO", "Acquisition cancelled by user")
            return
        
        # Start acquisition
        self._is_acquiring = True
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_progress.setText("Starting...")
        
        self.log_panel.append_log("Acquisition", "INFO", 
            f"Starting acquisition to: {output_structure['block_folder']}")
        self.log_panel.append_log("Acquisition", "INFO", 
            f"Total regions: {len(self._roi_list)}, Total tiles: {total_tiles}")
        
        # Run acquisition workflow
        self._run_acquisition_workflow(output_structure)
    
    def set_imaging_json_path(self, path: str):
        """Remember the imaging-JSON path the user loaded. focus_map.py (run as a
        subprocess) reads its regions + contours straight from this file."""
        self._imaging_json_path = path
        try:
            self.log_panel.append_log("Acquisition", "INFO", f"Imaging JSON set: {path}")
        except Exception:
            pass

    def _run_acquisition_workflow(self, output_structure: dict):
        """Drive the REAL acquisition pipeline (focus_map.py via main_live_qc.py)
        as a SUBPROCESS. The GUI builds a config JSON from the UI + the loaded
        imaging-JSON path, launches the pipeline with MSEM_CONFIG_JSON pointing
        at it, and mirrors the subprocess stdout + [Region]/[AcqTile]/[LiveQC]
        progress lines into the log / progress bar / map. Hardware is handed to
        the subprocess (engine polling + manual ops paused) for the duration."""
        import os
        from pathlib import Path
        from msem_control.engine.pipeline_runner import PipelineRunner, build_run_config

        if not self._imaging_json_path:
            self.log_panel.append_log(
                "Acquisition", "ERROR",
                "No imaging JSON loaded. Load a Zeiss experiment JSON (regions + "
                "support points) first — the pipeline reads its ROIs from that file, "
                "not from hand-drawn ROIs.")
            self._finish_acquisition()
            return

        # Pipeline scripts (main_live_qc.py + focus_map.py) live at the repo root,
        # alongside the msem_control/ package.
        repo_root = Path(__file__).resolve().parents[3]
        main_script = repo_root / "main_live_qc.py"
        if not main_script.is_file():
            self.log_panel.append_log(
                "Acquisition", "ERROR",
                f"main_live_qc.py not found at {main_script}. Deploy the pipeline "
                f"scripts (focus_map.py / main_live_qc.py / live_qc_support.py / "
                f"qc_algorithm.py) alongside the GUI.")
            self._finish_acquisition()
            return

        # Map UI controls -> pipeline config.
        prefix = self.edit_prefix.text().strip().rstrip("_") or "tile"
        ui = {
            "imaging_json_path": self._imaging_json_path,
            # point the wrapper at the co-located focus_map.py (override the
            # hardcoded D:\ default in main_live_qc.LIVE_QC_CONFIG)
            "acquire_script_path": str(repo_root / "focus_map.py"),
            "process_all_regions": True,
            "region_name_filter": "",  # empty -> acquire every region in the JSON
            "output_root": self.edit_output_path.text().strip(),
            "czi_name": output_structure.get("czi_name") or "acquisition",
            "prefix_base": prefix,
            "combo_format": self.combo_format.currentText().strip(),
            "acq_pixel_size_nm": self.spin_pixelsize.value(),
            "acq_scan_speed": self.combo_scanrate.currentData(),
            "mfov_overlap_pct": self.spin_overlap_mfov.value(),
            "sfov_overlap_um": self.spin_overlap_sfov.value(),
            "travel_pattern": self.combo_travel.currentText(),
            "travel_direction": self.combo_direction.currentText(),
            "start_position": self.combo_start.currentText(),
        }
        # Optional: a folder of prev_focusmap *.txt (matched per-section by the
        # pipeline). Only pass it when the user filled it in.
        pfm_dir = self.edit_prev_focusmap_dir.text().strip()
        if pfm_dir:
            ui["prev_focusmap_dir"] = pfm_dir
        config = build_run_config(ui)

        # Create + wire the runner once.
        if self._pipeline_runner is None:
            self._pipeline_runner = PipelineRunner(self)
            r = self._pipeline_runner
            r.log_line.connect(self._on_pipeline_log)
            r.region_total.connect(self._on_pipeline_region_total)
            r.region_started.connect(self._on_pipeline_region_started)
            r.tile_progress.connect(self._on_pipeline_tile_progress)
            r.qc_tile.connect(self._on_pipeline_qc_tile)
            r.region_done.connect(self._on_pipeline_region_done)
            r.finished.connect(self._on_pipeline_finished)
            r.error.connect(self._on_pipeline_error)
            r.beam_arc.connect(self._on_pipeline_beam_arc)

        # Hand hardware to the subprocess: stop engine polling + manual ops so the
        # two software components don't fight over the scope (Zeiss API warning).
        if self.engine:
            try:
                self.engine.pause_hardware()
            except Exception:
                pass

        self._run_total_regions = 0
        self._run_current_region_idx = 0
        config_path = str(repo_root / "msem_run_config.json")
        self.log_panel.append_log(
            "Acquisition", "INFO",
            f"Launching pipeline: {main_script.name}  (cwd={repo_root})")
        self.progress_bar.setValue(0)
        ok = self._pipeline_runner.start(
            config, workdir=str(repo_root), config_path=config_path,
            script="main_live_qc.py")
        if not ok:
            if self.engine:
                try:
                    self.engine.resume_hardware()
                except Exception:
                    pass
            self._finish_acquisition()

    # ------------------------------------------------------------------
    # PipelineRunner signal handlers (subprocess -> GUI)
    # ------------------------------------------------------------------
    def _on_pipeline_beam_arc(self, msg: str):
        # drive the top-level "Beam Arc" status indicator (the line itself is
        # already shown in the log via the normal log_line path)
        try:
            win = self.window()
            if hasattr(win, "status_panel"):
                win.status_panel.set_beam_arc(msg)
        except Exception:
            pass

    def _on_pipeline_log(self, line: str):
        """Mirror one subprocess output line into the GUI log, tagged by its
        [bracket] prefix and coloured by severity keywords."""
        tag = "Pipeline"
        if line.startswith("[") and "]" in line:
            tag = line[1:line.index("]")]
        low = line.lower()
        if "error" in low or "traceback" in low or "failed" in low or "crashed" in low:
            level = "ERROR"
        elif "warn" in low or "skip" in low:
            level = "WARNING"
        else:
            level = "INFO"
        self.log_panel.append_log(tag, level, line)

    def _on_pipeline_region_total(self, n: int):
        self._run_total_regions = n
        self.lbl_progress.setText(f"0 / {n} region(s)")

    def _on_pipeline_region_started(self, region_name: str, region_idx: int):
        self._run_current_region_idx = region_idx
        self.progress_bar.setValue(0)
        self.lbl_progress.setText(
            f"Region {region_idx}/{self._run_total_regions}: {region_name}")
        self.log_panel.append_log(
            "Acquisition", "INFO", f"Region {region_idx} START: {region_name}")

    def _on_pipeline_tile_progress(self, order: int, total: int):
        pct = int((order / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_progress.setText(
            f"Region {self._run_current_region_idx}/{self._run_total_regions} "
            f"— tile {order}/{total}")
        # Best-effort: recolour the MFOV hexagon green on the embedded map.
        # update_mfov_acquired lives on TileMapView (self.map_view). Region order
        # in the subprocess (SHORTEST_PATH) may differ from the GUI's region
        # indices, so this is approximate and guarded.
        try:
            self.map_view.update_mfov_acquired(self._run_current_region_idx - 1, order)
        except Exception:
            pass

    def _on_pipeline_qc_tile(self, tile_idx: int):
        # Reserved hook: a QC result was written for this tile. Could surface the
        # LiveQC dashboard / mark the tile distinctly. No-op for now.
        pass

    def _on_pipeline_region_done(self, region_name: str):
        self.log_panel.append_log("Acquisition", "SUCCESS", f"Region DONE: {region_name}")

    def _on_pipeline_finished(self, code: int):
        if self.engine:
            try:
                self.engine.resume_hardware()
            except Exception:
                pass
        if code == 0:
            self.progress_bar.setValue(100)
            self.log_panel.append_log("Acquisition", "SUCCESS", "Pipeline finished cleanly")
        else:
            self.log_panel.append_log(
                "Acquisition", "ERROR", f"Pipeline exited with code {code}")
        self._finish_acquisition()

    def _on_pipeline_error(self, msg: str):
        self.log_panel.append_log("Acquisition", "ERROR", msg)

    def _validate_support_points(self, af: 'Autofocus', region_idx: int, 
                                   sp_list: list) -> tuple:
        """Validate support points with autofocus (return passed, failed counts)"""
        from msem.stage import Stage
        
        passed = 0
        failed = 0
        
        # Highlight all support points in red (pending)
        for i, (x, y) in enumerate(sp_list):
            self.map_view.update_support_point_color(region_idx, i, 
                                                      QColor(255, 100, 100, 200))  # Light red (pending)
        QApplication.processEvents()
        
        # Move to each support point and run autofocus
        stage = Stage(af._conn)
        
        for i, (x, y) in enumerate(sp_list):
            if not self._is_acquiring:
                break
            
            # Update progress
            self.lbl_progress.setText(f"Autofocus: Support point {i+1}/{len(sp_list)}")
            
            self.log_panel.append_log("Autofocus", "INFO", 
                                      f"  Support point {i+1}/{len(sp_list)}: Moving to ({x:.3f}, {y:.3f})")
            QApplication.processEvents()
            
            # Move stage
            stage.move_xy(x, y, wait=True)
            
            # Run autofocus
            result = af.run()
            
            if result.success and result.confidence > 0:
                passed += 1
                # Update to bright green (passed)
                self.map_view.update_support_point_color(region_idx, i, 
                                                          QColor(50, 220, 80, 220))  # Bright green
                self.log_panel.append_log("Autofocus", "SUCCESS", 
                                          f"  Support point {i+1}: PASS (confidence={result.confidence:.3f})")
            else:
                failed += 1
                # Update to bright red (failed)
                self.map_view.update_support_point_color(region_idx, i, 
                                                          QColor(255, 60, 60, 220))  # Bright red
                self.log_panel.append_log("Autofocus", "ERROR", 
                                          f"  Support point {i+1}: FAIL ({result.message})")
            
            QApplication.processEvents()
        
        return passed, failed
    
    def _calculate_and_visualize_tiles(self, roi_acq: 'ROIAcquisition', 
                                         region_idx: int, points: list) -> list:
        """Calculate tiles and draw them in red on map"""
        from msem.roi_acquisition import ROIConfig, TravelPattern, TravelDirection, StartPosition
        
        # Set ROI config using UI-selected travel parameters
        config = ROIConfig(
            outer_mfov_overlap=self.spin_overlap_mfov.value() / 100.0,  # Convert % to fraction
            inner_sfov_overlap=self.spin_overlap_sfov.value(),  # Already in µm
            pixelsize=self.spin_pixelsize.value(),  # nm
            image_filetype="bmp",
            thumbnail_filetype="jpg",
            travel_pattern=self._get_travel_pattern(),
            travel_direction=self._get_travel_direction(),
            start_position=self._get_start_position(),
        )
        
        roi_acq.set_config(config)
        roi_acq.define_roi(points)
        
        try:
            tiles = roi_acq.calculate_tiles()
        except Exception as e:
            self.log_panel.append_log("ROI", "ERROR", f"Failed to calculate tiles: {e}")
            return []
        
        # Draw all tiles in red
        mfov_size_um = 110.0  # 91-beam MSEM MFOV diameter
        for tile_idx, tile in enumerate(tiles):
            tile_item = self.map_view.draw_tile_at_position(
                tile.x, tile.y, mfov_size_um, 
                QColor(255, 0, 0, 80),  # Red, semi-transparent
                tile_id=f"R{region_idx}_T{tile_idx}"
            )
            # Store reference for later update
            if not hasattr(self, '_tile_items'):
                self._tile_items = {}
            self._tile_items[(region_idx, tile_idx)] = tile_item
        
        return tiles
    
    def _update_tile_visualization(self, region_idx: int, tile_idx: int, completed: bool = True):
        """Update tile color (blue -> green when completed)"""
        # Update old-style tile items (if any)
        if hasattr(self, '_tile_items'):
            tile_item = self._tile_items.get((region_idx, tile_idx))
            if tile_item:
                if completed:
                    color = QColor(0, 200, 0, 80)  # Green (acquired)
                else:
                    color = QColor(100, 180, 255, 80)  # Blue (pending)
                self.map_view.update_tile_color(tile_item, color)
        
        # Update MFOV hexagon visualization (pending -> acquired)
        if completed:
            self.map_view.update_mfov_acquired(region_idx, tile_idx)
        
        # Force UI update
        QApplication.processEvents()
    
    def _finish_acquisition(self):
        """Reset UI after acquisition"""
        self._is_acquiring = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_progress.setText("Ready")
        
        # Update system status to idle
        if self.engine:
            self.engine._set_operation("idle")
    
    def _generate_output_structure(self) -> dict:
        """
        Generate output folder structure following the required format:
        
        Output Folder/
        └── {CZI_name}_{timestamp}/              <- Block folder
            ├── Region_1/                         <- Region subfolder
            │   ├── 0001/                         <- Tile subfolder
            │   │   ├── image_beam_01.bmp
            │   │   ├── image_beam_02.bmp
            │   │   └── ... (91 images total)
            │   ├── 0002/
            │   └── ...
            ├── Region_2/
            └── ...
        
        Returns:
            dict with 'base_folder', 'block_folder', 'timestamp', 'czi_name'
        """
        from datetime import datetime
        import os
        
        # Get base output folder from UI
        base_folder = self.edit_output_path.text()
        
        # Get CZI name if available (from loaded image)
        czi_name = "acquisition"
        if hasattr(self, '_current_image_path') and self._current_image_path:
            czi_name = os.path.splitext(os.path.basename(self._current_image_path))[0]
        
        # Generate timestamp: YYYYMMDD_HHMMSS
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Block folder name: {CZI_name}_{timestamp}
        block_folder_name = f"{czi_name}_{timestamp}"
        block_folder = os.path.join(base_folder, block_folder_name)
        
        # Pre-compute region folder paths
        region_folders = []
        for i in range(len(self._roi_list)):
            region_folder = os.path.join(block_folder, f"Region_{i + 1}")
            region_folders.append(region_folder)
        
        return {
            'base_folder': base_folder,
            'block_folder': block_folder,
            'block_folder_name': block_folder_name,
            'timestamp': timestamp,
            'czi_name': czi_name,
            'region_folders': region_folders
        }
    
    def _get_region_output_path(self, block_folder: str, region_idx: int) -> str:
        """Get output path for a specific region (1-indexed)"""
        return os.path.join(block_folder, f"Region_{region_idx}")
    
    def _get_tile_output_path(self, region_folder: str, tile_idx: int) -> str:
        """Get output path for a specific tile (with 91 beam images inside)"""
        import os
        return os.path.join(region_folder, f"{tile_idx:04d}")
    
    def _on_stop_acquisition(self):
        """Stop acquisition — interrupt the pipeline subprocess gracefully.
        focus_map/main_live_qc has a SIGINT handler that flushes the in-flight
        tile + shuts down the LiveQC subprocesses, so we send an interrupt and
        let the runner's finished signal drive the UI reset + hardware resume."""
        self.log_panel.append_log("Acquisition", "WARNING", "Stop requested")
        if self._pipeline_runner is not None and self._pipeline_runner.is_running():
            self._pipeline_runner.request_stop()
            self.lbl_progress.setText("Stopping…")
            # UI reset + engine.resume_hardware() happen in _on_pipeline_finished
            return
        # No subprocess running — just reset the UI.
        self._is_acquiring = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_progress.setText("Stopped")
        if self.engine:
            try:
                self.engine.resume_hardware()
            except Exception:
                pass
            self.engine._set_operation("idle")
        self.log_panel.append_log("Acquisition", "WARNING", "Acquisition stopped")
    
    def update_progress(self, current: int, total: int):
        """Update acquisition progress"""
        percent = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(percent)
        self.lbl_progress.setText(f"Tile {current}/{total}")
    
    def _on_load_roi(self):
        """Load ROI regions and support points from JSON/CSV file"""
        from PySide6.QtWidgets import QFileDialog
        import json
        
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load ROI Configuration",
            "",
            "JSON Files (*.json);;CSV Files (*.csv);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            # Load regions
            if 'regions' in data:
                self._roi_list = data['regions']
                self.map_view.clear_rois()
                
                for points in self._roi_list:
                    self.map_view.add_polygon_roi(points)
                
                self._update_roi_list()
                self.log_panel.append_log("ROI", "SUCCESS", 
                                          f"Loaded {len(self._roi_list)} region(s) from {file_path}")
            
            # Load support points (stored as um in JSON)
            if 'support_points' in data:
                self._support_points = {}
                for region_idx_str, sp_list_um in data['support_points'].items():
                    region_idx = int(region_idx_str)
                    
                    # Convert from um to pixel for internal storage
                    sp_list_pixel = []
                    for x_um, y_um in sp_list_um:
                        px, py = self.map_view.um_to_pixel(x_um, y_um)
                        sp_list_pixel.append((px, py))
                    
                    self._support_points[region_idx] = sp_list_pixel
                    
                    # Draw support points on map (using pixel coordinates)
                    self.map_view.draw_support_points(region_idx, sp_list_pixel, coordinates='pixel')
                
                total_sp = sum(len(sp) for sp in self._support_points.values())
                self.log_panel.append_log("Support Points", "SUCCESS", 
                                          f"Loaded {total_sp} support point(s)")
        
        except Exception as e:
            self.log_panel.append_log("ROI", "ERROR", f"Failed to load ROI: {e}")
            QMessageBox.warning(self, "Load Error", f"Failed to load ROI configuration:\n{e}")
    
    def _on_save_roi(self):
        """Save ROI regions and support points to JSON file"""
        from PySide6.QtWidgets import QFileDialog
        import json
        from datetime import datetime
        
        if not self._roi_list:
            QMessageBox.warning(self, "No ROI", "No regions to save")
            return
        
        # Generate default filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"roi_config_{timestamp}.json"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROI Configuration",
            default_name,
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            # Prepare data
            data = {
                'timestamp': timestamp,
                'regions': self._roi_list,
                'support_points': {}
            }
            
            # Add support points (convert dict keys to strings for JSON)
            for region_idx, sp_list in self._support_points.items():
                # Get current positions from map (in case they were dragged)
                current_positions = self.map_view.get_support_points(region_idx)
                if current_positions:
                    data['support_points'][str(region_idx)] = current_positions
            
            # Add acquisition parameters
            data['parameters'] = {
                'pixel_size_nm': self.spin_pixelsize.value(),
                'scan_rate': self.combo_scanrate.currentData(),
                'mfov_overlap_percent': self.spin_overlap_mfov.value(),
                'sfov_overlap_um': self.spin_overlap_sfov.value(),
                'travel_pattern': self.combo_travel.currentText(),
                'travel_direction': self.combo_direction.currentText(),
                'output_folder': self.edit_output_path.text(),
                'output_prefix': self.edit_prefix.text(),
                'output_format': self.combo_format.currentText()
            }
            
            # Save to file
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            total_sp = sum(len(sp) for sp in self._support_points.values())
            self.log_panel.append_log("ROI", "SUCCESS", 
                                      f"Saved {len(self._roi_list)} region(s) and {total_sp} support point(s) to {file_path}")
            QMessageBox.information(self, "Save Complete", 
                                    f"Configuration saved to:\n{file_path}")
        
        except Exception as e:
            self.log_panel.append_log("ROI", "ERROR", f"Failed to save ROI: {e}")
            QMessageBox.warning(self, "Save Error", f"Failed to save ROI configuration:\n{e}")
        self.lbl_tile_count.setText(str(current))
    
    def update_qc(self, result: dict):
        """Update QC display"""
        self.qc_panel.update_qc(
            result.get('focus_score', 0),
            result.get('contrast', 0)
        )
    
    def load_regions_from_json(self, regions_data: list):
        """Load regions and support points from Parameters Panel
        
        Args:
            regions_data: List of dicts with keys:
                - 'name': Region name
                - 'polygon_points': List of (x, y) tuples in µm
                - 'support_points': List of dicts with 'x', 'y', 'z', etc. in µm
                - 'z': Z coordinate
                - 'center_x', 'center_y': Center coordinates
        """
        try:
            # Store regions for redrawing when offset changes
            self._loaded_regions = regions_data
            
            # Clear existing ROIs
            self._roi_list.clear()
            self._support_points.clear()
            self.map_view.clear_rois()
            self.map_view.clear_support_points()
            self.regions_list.clear()
            
            # Load each region
            for idx, region in enumerate(regions_data):
                name = region['name']
                polygon_um = region['polygon_points']  # (x, y) tuples in µm
                support_points_um = region['support_points']  # List of dicts with x, y, z in µm
                
                # Convert polygon from µm to pixel coordinates
                polygon_px = []
                for x_um, y_um in polygon_um:
                    px, py = self.map_view.um_to_pixel(x_um, y_um)
                    polygon_px.append((px, py))
                
                # Add to internal list (pixel coordinates)
                self._roi_list.append(polygon_px)
                
                # Draw on map
                self.map_view.add_polygon_roi(polygon_px)
                
                # Convert support points from µm to pixel coordinates
                if support_points_um:
                    sp_px = []
                    for sp in support_points_um:
                        px, py = self.map_view.um_to_pixel(sp['x'], sp['y'])
                        sp_px.append((px, py))
                    
                    # Store and draw support points
                    self._support_points[idx] = sp_px
                    self.map_view.draw_support_points(idx, sp_px, coordinates='pixel')
                
                # Add to regions list with checkbox
                tiles_count = self._calculate_tiles_for_region(polygon_px)
                item = QListWidgetItem(f"{name} ({tiles_count} tiles)")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)  # Enabled by default
                self.regions_list.addItem(item)
            
            # Update status
            total_sp = sum(len(sp) for sp in self._support_points.values())
            msg = f"Loaded {len(regions_data)} region(s) and {total_sp} support point(s) from JSON"
            self.log_panel.append_log("ROI", "SUCCESS", msg)
            self.lbl_drawing_status.setText(f"{len(regions_data)} region(s) loaded from JSON")
            
            # Enable MFOV grid display to show tiles
            self.chk_show_mfov.setChecked(True)
            self.map_view.set_visualization_options(
                self.chk_show_overlap.isChecked(), 
                True
            )
            
            # Select first region if any
            if regions_data:
                self.regions_list.setCurrentRow(0)
            
            print(f"[AcquisitionPage] Loaded {len(regions_data)} regions from JSON:")
            for i, r in enumerate(regions_data):
                print(f"  Region {i+1}: {r['name']} - {len(r['polygon_points'])} vertices, {len(r['support_points'])} AF points")
        
        except Exception as e:
            self.log_panel.append_log("ROI", "ERROR", f"Failed to load regions from JSON: {e}")
            QMessageBox.warning(self, "Load Error", f"Failed to load regions:\n{e}")
            import traceback
            traceback.print_exc()
