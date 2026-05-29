from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QGridLayout, QDoubleSpinBox, QGroupBox, QSplitter, QScrollArea,
    QFormLayout, QFileDialog, QToolButton, QFrame, QSizePolicy, QLineEdit
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QMovie
from .base_page import BasePage
from ..widgets import CollapsibleBox, InteractiveMap, StatusIndicator
from msem_control.utils.roi_loader import load_rois_from_csv


class ControlPage(BasePage):
    def setup_ui(self):
        main_split = QSplitter(Qt.Horizontal)
        
        # === Left Panel (Tools) ===
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFixedWidth(350)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setAlignment(Qt.AlignTop)
        
        # 1. Stage Control
        stage_box = CollapsibleBox("Stage Control")
        stage_form = QFormLayout()
        
        # X, Y, Z spinboxes - 4 decimals stored, 3 displayed
        self.spin_x = QDoubleSpinBox()
        self.spin_x.setRange(-100000, 100000)
        self.spin_x.setDecimals(4)  # Store 4 decimals
        self.spin_x.setSuffix(" µm")
        
        self.spin_y = QDoubleSpinBox()
        self.spin_y.setRange(-100000, 100000)
        self.spin_y.setDecimals(4)
        self.spin_y.setSuffix(" µm")
        
        self.spin_z = QDoubleSpinBox()
        self.spin_z.setRange(0, 50000)
        self.spin_z.setDecimals(4)
        self.spin_z.setSuffix(" µm")
        self.spin_z.setEnabled(False)  # Z is read-only for safety
        
        # Current position display
        self.lbl_current_pos = QLabel("Current: X=---, Y=---, Z=---")
        self.lbl_current_pos.setStyleSheet("color: #888; font-size: 11px;")
        
        # Buttons
        self.btn_move_xy = QPushButton("Move XY")
        self.btn_move_xy.clicked.connect(self.on_move_xy)
        
        self.btn_read_pos = QPushButton("Read Position")
        self.btn_read_pos.clicked.connect(self.on_read_position)
        
        stage_form.addRow("Target X:", self.spin_x)
        stage_form.addRow("Target Y:", self.spin_y)
        stage_form.addRow("Current Z:", self.spin_z)
        stage_form.addRow(self.lbl_current_pos)
        
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_move_xy)
        btn_row.addWidget(self.btn_read_pos)
        stage_form.addRow(btn_row)
        
        stage_box.set_content_layout(stage_form)
        left_layout.addWidget(stage_box)

        # (Live Mode removed — single/full live never worked through the API;
        #  the live image only renders in ZEN. Use ZEN for live preview.)

        # 3. Focus & Stig
        focus_box = CollapsibleBox("Focus && Stigmation")
        focus_form = QFormLayout()
        
        self.spin_af_range = QDoubleSpinBox()
        self.spin_af_range.setRange(0, 500)
        self.spin_af_range.setValue(150.0)
        self.spin_af_range.setSuffix(" µm")
        
        self.btn_af = QPushButton("Run Autofocus")
        self.btn_af.clicked.connect(lambda: self.engine.run_autofocus())
        
        self.btn_stig = QPushButton("Run Autostig")
        self.btn_stig.clicked.connect(lambda: self.engine.run_autostig())
        
        focus_form.addRow("AF Range:", self.spin_af_range)
        focus_form.addRow(self.btn_af)
        focus_form.addRow(self.btn_stig)
        focus_box.set_content_layout(focus_form)
        left_layout.addWidget(focus_box)
        
        # 4. Wafer Calibration
        calib_box = CollapsibleBox("Wafer Calibration")
        calib_layout = QVBoxLayout()
        self.btn_shuffle = QPushButton("Shuffle && Find")
        self.btn_shuffle.setToolTip("Run wafer shuffle and find procedure")
        calib_layout.addWidget(self.btn_shuffle)
        calib_box.set_content_layout(calib_layout)
        left_layout.addWidget(calib_box)
        
        # 5. Map Calibration
        scale_box = CollapsibleBox("Map Calibration")
        scale_form = QFormLayout()
        
        self.spin_pixel_size = QDoubleSpinBox()
        self.spin_pixel_size.setRange(0.01, 1000)
        self.spin_pixel_size.setValue(1.0)
        self.spin_pixel_size.setDecimals(4)
        self.spin_pixel_size.setSuffix(" µm/px")
        self.spin_pixel_size.valueChanged.connect(self.on_calibration_changed)
        
        self.spin_offset_x = QDoubleSpinBox()
        self.spin_offset_x.setRange(-1000000, 1000000)
        self.spin_offset_x.setDecimals(4)
        self.spin_offset_x.setValue(0)
        self.spin_offset_x.setSuffix(" µm")
        
        self.spin_offset_y = QDoubleSpinBox()
        self.spin_offset_y.setRange(-1000000, 1000000)
        self.spin_offset_y.setDecimals(4)
        self.spin_offset_y.setValue(0)
        self.spin_offset_y.setSuffix(" µm")
        
        scale_form.addRow("Pixel Size:", self.spin_pixel_size)
        scale_form.addRow("Offset X:", self.spin_offset_x)
        scale_form.addRow("Offset Y:", self.spin_offset_y)
        scale_box.set_content_layout(scale_form)
        left_layout.addWidget(scale_box)
        
        left_scroll.setWidget(left_widget)
        
        # === Center: Two-panel view (Map + Live) ===
        center_split = QSplitter(Qt.Horizontal)
        
        # Left: Optical Map
        map_container = QWidget()
        map_layout = QVBoxLayout(map_container)
        
        map_toolbar = QHBoxLayout()
        btn_load_map = QPushButton("Load Map")
        btn_load_map.clicked.connect(self.load_map)
        btn_load_roi = QPushButton("Load ROI (CSV)")
        btn_load_roi.clicked.connect(self.load_roi)
        btn_clear_roi = QPushButton("Clear ROIs")
        btn_clear_roi.clicked.connect(lambda: self.map_view.clear_rois())
        
        map_toolbar.addWidget(btn_load_map)
        map_toolbar.addWidget(btn_load_roi)
        map_toolbar.addWidget(btn_clear_roi)
        map_toolbar.addStretch()
        
        map_label = QLabel("Optical Map")
        map_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        
        hint_label = QLabel("Double-click to move stage")
        hint_label.setStyleSheet("color: #888; font-size: 10px;")
        
        self.map_view = InteractiveMap()
        self.map_view.stage_moved.connect(self.on_map_clicked)
        
        map_layout.addWidget(map_label)
        map_layout.addLayout(map_toolbar)
        map_layout.addWidget(hint_label)
        map_layout.addWidget(self.map_view)
        
        # (Live View / scintillator pane removed along with single/full live —
        #  it only ever worked in ZEN. The optical map now takes the full width.)
        center_split.addWidget(map_container)

        main_split.addWidget(left_scroll)
        main_split.addWidget(center_split)
        main_split.setStretchFactor(1, 1)

        self.layout.addWidget(main_split)

        # Connect engine signals for real-time position updates
        if self.engine and hasattr(self.engine, 'on_position_update'):
            self.engine.on_position_update = self._on_position_update

    def load_map(self):
        """Load optical map image (PNG, JPG, TIF, CZI)"""
        fname, _ = QFileDialog.getOpenFileName(
            self, "Open Optical Image", "", 
            "All Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.czi);;PNG (*.png);;JPEG (*.jpg *.jpeg);;TIFF (*.tif *.tiff);;CZI (*.czi)"
        )
        if fname:
            if fname.lower().endswith('.czi'):
                # CZI requires special handling
                print(f"Loading CZI: {fname}")
                # Try to use aicspylibczi or czifile
                try:
                    self._load_czi(fname)
                except Exception as e:
                    print(f"CZI loading failed: {e}")
                    print("Install: pip install aicspylibczi or czifile")
            else:
                if self.map_view.load_image(fname):
                    print(f"Loaded map: {fname}")
                    self.on_calibration_changed()

    def _load_czi(self, filepath):
        """Load CZI file"""
        try:
            # Try aicspylibczi first
            from aicspylibczi import CziFile
            import numpy as np
            from PySide6.QtGui import QImage, QPixmap
            
            czi = CziFile(filepath)
            # Get the first scene/channel
            img_data, _ = czi.read_image()
            
            # Convert to 8-bit if needed
            if img_data.dtype != np.uint8:
                img_data = ((img_data - img_data.min()) / (img_data.max() - img_data.min()) * 255).astype(np.uint8)
            
            # Handle dimensions (remove singleton dims)
            img_data = np.squeeze(img_data)
            if len(img_data.shape) == 3:
                img_data = img_data[0]  # Take first channel/scene
            
            # Convert to QImage
            h, w = img_data.shape
            qimg = QImage(img_data.data, w, h, w, QImage.Format_Grayscale8)
            pixmap = QPixmap.fromImage(qimg)
            
            # Load into map
            self.map_view.scene.clear()
            from PySide6.QtWidgets import QGraphicsPixmapItem
            self.map_view.pixmap_item = QGraphicsPixmapItem(pixmap)
            self.map_view.scene.addItem(self.map_view.pixmap_item)
            self.map_view.setSceneRect(self.map_view.pixmap_item.boundingRect())
            self.map_view.fitInView(self.map_view.pixmap_item, Qt.KeepAspectRatio)
            
            print(f"CZI loaded: {w}x{h}")
            return True
            
        except ImportError:
            try:
                # Try czifile as fallback
                import czifile
                import numpy as np
                from PySide6.QtGui import QImage, QPixmap
                
                with czifile.CziFile(filepath) as czi:
                    img_data = czi.asarray()
                
                # Squeeze and take first plane
                img_data = np.squeeze(img_data)
                if len(img_data.shape) > 2:
                    img_data = img_data[0] if len(img_data.shape) == 3 else img_data[0, 0]
                
                # Normalize to 8-bit
                if img_data.dtype != np.uint8:
                    img_data = ((img_data - img_data.min()) / (img_data.max() - img_data.min()) * 255).astype(np.uint8)
                
                h, w = img_data.shape
                qimg = QImage(img_data.data, w, h, w, QImage.Format_Grayscale8)
                pixmap = QPixmap.fromImage(qimg)
                
                self.map_view.scene.clear()
                from PySide6.QtWidgets import QGraphicsPixmapItem
                self.map_view.pixmap_item = QGraphicsPixmapItem(pixmap)
                self.map_view.scene.addItem(self.map_view.pixmap_item)
                self.map_view.setSceneRect(self.map_view.pixmap_item.boundingRect())
                self.map_view.fitInView(self.map_view.pixmap_item, Qt.KeepAspectRatio)
                
                print(f"CZI loaded (czifile): {w}x{h}")
                return True
                
            except ImportError:
                raise ImportError("No CZI library found. Install: pip install aicspylibczi or pip install czifile")

    def load_roi(self):
        fname, _ = QFileDialog.getOpenFileName(
            self, "Open ROI CSV", "", "CSV Files (*.csv)"
        )
        if fname:
            try:
                polygons = load_rois_from_csv(fname)
                self.map_view.clear_rois()
                
                colors = [
                    QColor(255, 100, 0),   # Orange
                    QColor(0, 200, 100),   # Green
                    QColor(100, 150, 255), # Blue
                    QColor(255, 50, 150),  # Pink
                    QColor(200, 200, 0),   # Yellow
                ]
                
                for i, poly in enumerate(polygons):
                    color = colors[i % len(colors)]
                    self.map_view.add_polygon_roi(poly, color)
                    
                print(f"Loaded {len(polygons)} polygons from {fname}")
            except Exception as e:
                print(f"Failed to load ROI: {e}")
                import traceback
                traceback.print_exc()

    # === Stage Control ===

    def on_map_clicked(self, x, y):
        """Called when user double-clicks on map"""
        self.spin_x.setValue(x)
        self.spin_y.setValue(y)
        self.engine.move_stage(x, y)
        
    def on_move_xy(self):
        """Move stage to spinbox values"""
        x = self.spin_x.value()
        y = self.spin_y.value()
        self.engine.move_stage(x, y)
        
    def on_read_position(self):
        """Read current position from hardware and update Target X/Y"""
        if not self.engine.is_connected():
            print("Not connected - cannot read position")
            return
            
        pos = self.engine.get_position()
        self.spin_x.setValue(pos.x)
        self.spin_y.setValue(pos.y)
        self.spin_z.setValue(pos.z)
        self.lbl_current_pos.setText(f"Current: X={pos.x:.3f}, Y={pos.y:.3f}, Z={pos.z:.3f}")
        print(f"Read position: X={pos.x:.4f}, Y={pos.y:.4f}, Z={pos.z:.4f}")

    def on_calibration_changed(self):
        """Update map calibration when values change"""
        self.map_view.set_calibration(
            self.spin_pixel_size.value(),
            self.spin_offset_x.value(),
            self.spin_offset_y.value()
        )
        
    def _on_position_update(self, x: float, y: float, z: float):
        """Called during stage movement for real-time position updates"""
        self.lbl_current_pos.setText(f"Current: X={x:.4f}, Y={y:.4f}, Z={z:.4f}")
        self.spin_z.setValue(z)
        
        # Update crosshair on map to follow stage movement
        self.map_view.draw_crosshair_um(x, y)
    
    def update_position(self, x: float, y: float, z: float):
        """Called by engine when position updates (background polling)"""
        self.lbl_current_pos.setText(f"Current: X={x:.4f}, Y={y:.4f}, Z={z:.4f}")
        self.spin_z.setValue(z)
