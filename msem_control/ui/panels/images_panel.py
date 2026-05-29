"""
Images and Documents Panel

Displays uploaded images/documents with thumbnails.
Supports drag-and-drop to other components.

For large CZI files (several GB), thumbnails are generated using:
- Low resolution pyramid level if available
- Subsampling to reduce memory usage
- Background thread loading to keep UI responsive
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QFrame, QGridLayout, QFileDialog, QMenu,
    QDialog, QRadioButton, QButtonGroup, QDoubleSpinBox,
    QDialogButtonBox, QProgressBar, QApplication
)
from PySide6.QtCore import Qt, Signal, QMimeData, QSize, QThread, QObject
from PySide6.QtGui import QPixmap, QDrag, QIcon, QPainter, QColor, QImage
import os
import numpy as np


class ThumbnailLoader(QObject):
    """Background thread for loading large image thumbnails"""
    finished = Signal(str, QPixmap)  # filepath, pixmap
    progress = Signal(str, int)  # filepath, percent
    error = Signal(str, str)  # filepath, error_message
    
    def __init__(self, filepath: str, thumb_size: int = 80):
        super().__init__()
        self.filepath = filepath
        self.thumb_size = thumb_size
    
    def run(self):
        """Load thumbnail in background"""
        try:
            ext = os.path.splitext(self.filepath)[1].lower()
            
            if ext == '.czi':
                pixmap = self._load_czi_thumbnail()
            elif ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff']:
                pixmap = self._load_image_thumbnail()
            else:
                pixmap = None
            
            if pixmap and not pixmap.isNull():
                self.finished.emit(self.filepath, pixmap)
            else:
                self.error.emit(self.filepath, "Failed to load")
                
        except Exception as e:
            self.error.emit(self.filepath, str(e))
    
    def _load_czi_thumbnail(self) -> QPixmap:
        """Load CZI thumbnail efficiently for large files (including mosaic)"""
        self.progress.emit(self.filepath, 10)
        
        try:
            from aicspylibczi import CziFile
            
            czi = CziFile(self.filepath)
            self.progress.emit(self.filepath, 30)
            
            # Check if mosaic
            if czi.is_mosaic():
                # Mosaic CZI - use read_mosaic with low scale
                bbox = czi.get_mosaic_bounding_box()
                max_dim = max(bbox.w, bbox.h)
                
                # Calculate scale to get ~500px thumbnail
                scale = min(1.0, 500.0 / max_dim)
                
                self.progress.emit(self.filepath, 50)
                
                # Read mosaic at reduced scale
                mosaic = czi.read_mosaic(C=0, scale_factor=scale)
                
                self.progress.emit(self.filepath, 70)
                
                # Extract 2D image from mosaic shape (1, H, W, A)
                if len(mosaic.shape) == 4:
                    img = mosaic[0, :, :, 0]
                elif len(mosaic.shape) == 3:
                    # Could be (C, H, W) or (H, W, C)
                    if mosaic.shape[0] <= 3:  # Likely (C, H, W)
                        img = mosaic[0, :, :]
                    else:  # Likely (H, W, C)
                        img = mosaic[:, :, 0]
                else:
                    img = np.squeeze(mosaic)
                    
            else:
                # Regular CZI
                dims = czi.get_dims_shape()[0]
                
                # Handle tuple dimensions
                x_dim = dims.get('X', (0, 1000))
                y_dim = dims.get('Y', (0, 1000))
                max_x = x_dim[1] if isinstance(x_dim, tuple) else x_dim
                max_y = y_dim[1] if isinstance(y_dim, tuple) else y_dim
                max_dim = max(max_x, max_y)
                
                self.progress.emit(self.filepath, 50)
                
                # Read image
                result = czi.read_image()
                img = result[0] if isinstance(result, tuple) else result
                
                self.progress.emit(self.filepath, 70)
                
                img = np.squeeze(img)
                if len(img.shape) > 2:
                    img = img[0] if len(img.shape) == 3 else img[0, 0]
                
                # Subsample if needed
                if max_dim > 500:
                    factor = max(1, max_dim // 500)
                    img = img[::factor, ::factor]
            
            self.progress.emit(self.filepath, 85)
            
            # Normalize to 8-bit
            if img.dtype != np.uint8:
                pmin, pmax = np.percentile(img, (1, 99))
                img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
            
            # Convert to QPixmap
            h, w = img.shape
            img = np.ascontiguousarray(img)
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
            pixmap = QPixmap.fromImage(qimg.copy())
            
            # Scale to thumbnail size
            pixmap = pixmap.scaled(
                self.thumb_size, self.thumb_size,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            
            self.progress.emit(self.filepath, 100)
            return pixmap
            
        except ImportError:
            # Try czifile as fallback
            try:
                import czifile
                
                self.progress.emit(self.filepath, 30)
                
                with czifile.CziFile(self.filepath) as czi:
                    img = czi.asarray()
                
                self.progress.emit(self.filepath, 60)
                
                img = np.squeeze(img)
                if len(img.shape) > 2:
                    img = img[0] if len(img.shape) == 3 else img[0, 0]
                
                # Subsample large images
                max_dim = max(img.shape)
                if max_dim > 500:
                    factor = max_dim // 500
                    img = img[::factor, ::factor]
                
                self.progress.emit(self.filepath, 80)
                
                if img.dtype != np.uint8:
                    pmin, pmax = np.percentile(img, (1, 99))
                    img = np.clip((img - pmin) / (pmax - pmin + 1e-6) * 255, 0, 255).astype(np.uint8)
                
                h, w = img.shape
                img = np.ascontiguousarray(img)
                qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
                pixmap = QPixmap.fromImage(qimg.copy())
                
                pixmap = pixmap.scaled(
                    self.thumb_size, self.thumb_size,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                
                self.progress.emit(self.filepath, 100)
                return pixmap
                
            except Exception as e:
                raise Exception(f"CZI load failed: {e}")
    
    def _load_image_thumbnail(self) -> QPixmap:
        """Load regular image thumbnail"""
        self.progress.emit(self.filepath, 50)
        
        pixmap = QPixmap(self.filepath)
        if pixmap.isNull():
            return None
        
        self.progress.emit(self.filepath, 80)
        
        pixmap = pixmap.scaled(
            self.thumb_size, self.thumb_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        
        self.progress.emit(self.filepath, 100)
        return pixmap


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


class ThumbnailWidget(QFrame):
    """Single thumbnail item with drag support and async loading"""
    
    double_clicked = Signal(str)  # filepath
    loading_finished = Signal()
    
    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self._is_loading = False
        self._loader_thread = None
        
        self.setFixedSize(100, 130)
        self.setCursor(Qt.OpenHandCursor)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            ThumbnailWidget {
                background-color: #2a2a2a;
                border: 1px solid #3e3e3e;
                border-radius: 4px;
            }
            ThumbnailWidget:hover {
                border: 1px solid #007acc;
                background-color: #333333;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        
        # Thumbnail image
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(90, 80)
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self.thumb_label.setStyleSheet("background-color: #1e1e1e; border-radius: 2px;")
        
        # Progress bar (hidden by default)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setRange(0, 100)
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
        
        # Filename label
        name_label = QLabel(self._truncate_name(self.filename, 12))
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setStyleSheet("font-size: 9px; color: #aaa;")
        name_label.setToolTip(self.filename)
        
        layout.addWidget(self.thumb_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(name_label)
        
        # Start loading thumbnail
        self._start_loading()
    
    def _truncate_name(self, name: str, max_len: int) -> str:
        if len(name) <= max_len:
            return name
        ext = os.path.splitext(name)[1]
        base = name[:max_len - len(ext) - 3]
        return f"{base}...{ext}"
    
    def _start_loading(self):
        """Start background loading of thumbnail"""
        self._is_loading = True
        self.progress_bar.show()
        self.progress_bar.setValue(0)
        
        # Show loading indicator
        self._show_loading_icon()
        
        # Create loader
        self._loader = ThumbnailLoader(self.filepath, 80)
        self._loader_thread = QThread()
        self._loader.moveToThread(self._loader_thread)
        
        # Connect signals
        self._loader_thread.started.connect(self._loader.run)
        self._loader.finished.connect(self._on_thumbnail_loaded)
        self._loader.progress.connect(self._on_progress)
        self._loader.error.connect(self._on_load_error)
        self._loader.finished.connect(self._loader_thread.quit)
        self._loader.error.connect(self._loader_thread.quit)
        
        # Start thread
        self._loader_thread.start()
    
    def _show_loading_icon(self):
        """Show loading placeholder"""
        ext = os.path.splitext(self.filepath)[1].lower()
        icon_text = ext.upper().replace('.', '')
        
        pixmap = QPixmap(60, 60)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw box with loading indicator color
        painter.setBrush(QColor("#2a4a6a"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 60, 60, 8, 8)
        
        # Draw text
        painter.setPen(QColor("#888888"))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, f"{icon_text}\n...")
        painter.end()
        
        self.thumb_label.setPixmap(pixmap)
    
    def _on_progress(self, filepath: str, percent: int):
        """Update progress bar"""
        if filepath == self.filepath:
            self.progress_bar.setValue(percent)
    
    def _on_thumbnail_loaded(self, filepath: str, pixmap: QPixmap):
        """Handle successful thumbnail load"""
        if filepath == self.filepath:
            self._is_loading = False
            self.progress_bar.hide()
            self.thumb_label.setPixmap(pixmap)
            self.loading_finished.emit()
    
    def _on_load_error(self, filepath: str, error: str):
        """Handle load error - show file type icon"""
        if filepath == self.filepath:
            self._is_loading = False
            self.progress_bar.hide()
            self._show_file_icon(os.path.splitext(filepath)[1].lower())
            print(f"Thumbnail load error for {filepath}: {error}")
    
    def _show_file_icon(self, ext: str):
        """Show file type icon as fallback"""
        icon_text = ext.upper().replace('.', '')
        
        pixmap = QPixmap(60, 60)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw box
        painter.setBrush(QColor("#3a506b"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 60, 60, 8, 8)
        
        # Draw text
        painter.setPen(QColor("#ffffff"))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, icon_text)
        painter.end()
        
        self.thumb_label.setPixmap(pixmap)
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            if hasattr(self, '_drag_start_pos'):
                distance = (event.pos() - self._drag_start_pos).manhattanLength()
                if distance > 10:
                    self._start_drag()
        super().mouseMoveEvent(event)
    
    def _start_drag(self):
        """Initiate drag operation"""
        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setUrls([self.filepath])
        mime_data.setText(self.filepath)
        mime_data.setData("application/x-msem-image", self.filepath.encode())
        drag.setMimeData(mime_data)
        
        # Create drag pixmap
        if self.thumb_label.pixmap():
            drag.setPixmap(self.thumb_label.pixmap().scaled(50, 50, Qt.KeepAspectRatio))
        
        drag.exec_(Qt.CopyAction)
    
    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.filepath)
        super().mouseDoubleClickEvent(event)


class ImagesPanel(QWidget):
    """Panel for managing images and documents"""
    
    image_dropped = Signal(str, bool, float)  # filepath, use_fullscale, scale_percent
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.thumbnails = []
        self.setup_ui()
    
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        
        # Header with Open button
        header = QHBoxLayout()
        
        btn_open = QPushButton("Open")
        btn_open.setIcon(QIcon.fromTheme("document-open"))
        btn_open.clicked.connect(self.open_files)
        
        btn_clear = QPushButton("Clear All")
        btn_clear.setStyleSheet("background-color: #5a5a5a;")
        btn_clear.clicked.connect(self.clear_all)
        
        header.addWidget(btn_open)
        header.addWidget(btn_clear)
        header.addStretch()
        
        layout.addLayout(header)
        
        # Info label
        self.info_label = QLabel("No files loaded")
        self.info_label.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(self.info_label)
        
        # Thumbnail grid in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(8)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        scroll.setWidget(self.grid_widget)
        layout.addWidget(scroll, 1)
        
        # Hint
        hint = QLabel("Drag images to Tiles Setup")
        hint.setStyleSheet("color: #666; font-size: 10px;")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)
    
    def open_files(self):
        """Open file dialog to add images"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Images/Documents",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.czi);;All Files (*)"
        )
        
        for filepath in files:
            self.add_file(filepath)
    
    def add_file(self, filepath: str):
        """Add a file to the panel"""
        if not os.path.exists(filepath):
            return
            
        # Check if already added
        for thumb in self.thumbnails:
            if thumb.filepath == filepath:
                return
        
        thumb = ThumbnailWidget(filepath)
        thumb.double_clicked.connect(self._on_thumb_double_click)
        
        self.thumbnails.append(thumb)
        self._update_grid()
        self._update_info()
    
    def _update_grid(self):
        """Reorganize thumbnail grid"""
        # Clear grid
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            # Don't delete the widget, just remove from layout
        
        # Add thumbnails in 2-column grid
        cols = 2
        for i, thumb in enumerate(self.thumbnails):
            row = i // cols
            col = i % cols
            self.grid_layout.addWidget(thumb, row, col)
    
    def _update_info(self):
        """Update info label"""
        count = len(self.thumbnails)
        if count == 0:
            self.info_label.setText("No files loaded")
        elif count == 1:
            self.info_label.setText("1 file loaded")
        else:
            self.info_label.setText(f"{count} files loaded")
    
    def _on_thumb_double_click(self, filepath):
        """Handle double-click on thumbnail"""
        print(f"Open: {filepath}")
        # Could open in external viewer or show preview
    
    def clear_all(self):
        """Remove all thumbnails"""
        for thumb in self.thumbnails:
            thumb.deleteLater()
        self.thumbnails.clear()
        self._update_info()
    
    def remove_file(self, filepath: str):
        """Remove a specific file"""
        for thumb in self.thumbnails[:]:
            if thumb.filepath == filepath:
                self.thumbnails.remove(thumb)
                thumb.deleteLater()
                break
        self._update_grid()
        self._update_info()

