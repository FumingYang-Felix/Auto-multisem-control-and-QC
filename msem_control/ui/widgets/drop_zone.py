"""
Drop Zone Widget

A widget that accepts dropped images and shows preview.
Used for Tiles Setup in Acquisition page.
"""
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QLabel, QHBoxLayout, QPushButton,
    QDialog, QRadioButton, QButtonGroup, QDoubleSpinBox,
    QDialogButtonBox, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
import os


class ImageScaleDialog(QDialog):
    """Dialog to choose image scaling option when dropping"""
    
    def __init__(self, filename: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image Import Options")
        self.setModal(True)
        self.setMinimumWidth(350)
        
        layout = QVBoxLayout(self)
        
        # Info
        info = QLabel(f"Import: {os.path.basename(filename)}")
        info.setStyleSheet("font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(info)
        
        # Options
        self.radio_fullscale = QRadioButton("Use full-scale image")
        self.radio_fullscale.setChecked(True)
        
        self.radio_scaled = QRadioButton("Use scaling (%)")
        
        self.spin_scale = QDoubleSpinBox()
        self.spin_scale.setRange(1, 100)
        self.spin_scale.setValue(50)
        self.spin_scale.setSuffix(" %")
        self.spin_scale.setEnabled(False)
        
        # Connect radio to spinbox
        self.radio_scaled.toggled.connect(self.spin_scale.setEnabled)
        
        self.button_group = QButtonGroup(self)
        self.button_group.addButton(self.radio_fullscale, 1)
        self.button_group.addButton(self.radio_scaled, 2)
        
        layout.addWidget(self.radio_fullscale)
        
        scale_layout = QHBoxLayout()
        scale_layout.addWidget(self.radio_scaled)
        scale_layout.addWidget(self.spin_scale)
        scale_layout.addStretch()
        layout.addLayout(scale_layout)
        
        layout.addSpacing(20)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        self.setStyleSheet("""
            QDialog {
                background-color: #2d2d2d;
            }
            QLabel {
                color: #cccccc;
            }
            QRadioButton {
                color: #cccccc;
            }
        """)
    
    def get_result(self):
        """Returns (use_fullscale, scale_percent)"""
        if self.radio_fullscale.isChecked():
            return (True, 100.0)
        else:
            return (False, self.spin_scale.value())


class DropZone(QFrame):
    """
    Drop zone for receiving dragged images.
    
    Shows a dashed border when empty, and the image preview when loaded.
    """
    
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
        
        # Clear button (hidden when empty)
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
        
        layout.addWidget(self.view)
        layout.addWidget(self.info_label)
        
        # Position the empty label and clear button using absolute positioning
        self.empty_label.setParent(self.view)
        self.btn_clear.setParent(self.view)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Center the empty label
        if self.empty_label.isVisible():
            self.empty_label.setGeometry(
                0, 0, 
                self.view.width(), 
                self.view.height()
            )
        # Position clear button at top-right
        if self.btn_clear.isVisible():
            self.btn_clear.move(self.view.width() - 30, 6)
    
    def _update_style(self, has_image: bool):
        """Update border style based on state"""
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
        """Handle drag enter - check if we accept the data"""
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
        """Handle drag leave"""
        self._update_style(self.current_image is not None)
    
    def dropEvent(self, event):
        """Handle drop - load the image"""
        mime = event.mimeData()
        filepath = None
        
        # Try to get filepath from different MIME types
        if mime.hasFormat("application/x-msem-image"):
            filepath = bytes(mime.data("application/x-msem-image")).decode()
        elif mime.hasText():
            filepath = mime.text()
        elif mime.hasUrls():
            urls = mime.urls()
            if urls:
                filepath = urls[0].toLocalFile()
        
        if filepath and os.path.exists(filepath):
            # Show scale dialog
            dialog = ImageScaleDialog(filepath, self)
            if dialog.exec() == QDialog.Accepted:
                use_fullscale, scale_percent = dialog.get_result()
                self.load_image(filepath, use_fullscale, scale_percent)
        
        self._update_style(self.current_image is not None)
        event.acceptProposedAction()
    
    def load_image(self, filepath: str, use_fullscale: bool = True, scale_percent: float = 100.0):
        """Load an image into the drop zone"""
        self.current_path = filepath
        self.use_fullscale = use_fullscale
        self.scale_percent = scale_percent
        
        # Try to load the image
        ext = os.path.splitext(filepath)[1].lower()
        
        pixmap = None
        if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff']:
            pixmap = QPixmap(filepath)
        elif ext == '.czi':
            # Try to load CZI
            pixmap = self._load_czi_preview(filepath)
        
        if pixmap and not pixmap.isNull():
            self.scene.clear()
            
            # Apply scaling if needed
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
            
            # Update UI
            self.empty_label.hide()
            self.btn_clear.show()
            self.btn_clear.raise_()
            
            # Update info
            w, h = pixmap.width(), pixmap.height()
            scale_info = "Full scale" if use_fullscale else f"{scale_percent:.0f}%"
            self.info_label.setText(f"{os.path.basename(filepath)} | {w}x{h} | {scale_info}")
            self.info_label.show()
            
            self._update_style(True)
            self.image_loaded.emit(filepath, use_fullscale, scale_percent)
            
            # Position clear button
            self.btn_clear.move(self.view.width() - 30, 6)
        else:
            print(f"Failed to load image: {filepath}")
    
    def _load_czi_preview(self, filepath: str):
        """Load CZI file preview"""
        try:
            from aicspylibczi import CziFile
            import numpy as np
            from PySide6.QtGui import QImage
            
            czi = CziFile(filepath)
            dims = czi.get_dims_shape()[0]
            
            # Read a single plane
            img, _ = czi.read_image()
            img = np.squeeze(img)
            
            if len(img.shape) > 2:
                img = img[0] if len(img.shape) == 3 else img[0, 0]
            
            # Normalize
            if img.dtype != np.uint8:
                img = ((img - img.min()) / (img.max() - img.min()) * 255).astype(np.uint8)
            
            h, w = img.shape
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
            return QPixmap.fromImage(qimg)
            
        except Exception as e:
            print(f"CZI load error: {e}")
            return None
    
    def clear_image(self):
        """Clear the current image"""
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
        """Get current image info"""
        if self.current_path:
            return {
                'path': self.current_path,
                'use_fullscale': self.use_fullscale,
                'scale_percent': self.scale_percent
            }
        return None

