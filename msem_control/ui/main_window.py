import sys
import json
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QStackedWidget, QTabBar,
    QDockWidget, QScrollArea, QMessageBox
)
from PySide6.QtCore import Qt, QThread, Signal

from ..engine.workflow import WorkflowEngine
from .widgets import LogPanel
from .pages.control_page import ControlPage
from .pages.acquisition_page import AcquisitionPage
from .pages.himc_page import HiMcPage
from .panels.parameters_panel import ParametersPanel
from .panels.status_panel import StatusPanel
from .panels.images_panel import ImagesPanel


class MainWindow(QMainWindow):
    # Signals for thread-safe UI updates
    operation_changed = Signal(str, int)  # status, progress
    position_changed = Signal(float, float, float)  # x, y, z
    all_status_changed = Signal(dict)  # comprehensive status (from poll thread)
    
    def __init__(self, engine: WorkflowEngine):
        super().__init__()
        self.engine = engine
        
        # Connect signals to slots (for thread-safe UI updates)
        self.operation_changed.connect(self._handle_operation_slot)
        self.position_changed.connect(self._handle_position_slot)
        self.all_status_changed.connect(self._handle_all_status_slot)
        
        self.setWindowTitle("MultiSEM Professional Control")
        self.resize(1600, 1000)
        
        # Central Widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main Layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # === 1. Top Navigation Bar ===
        self.create_top_bar(main_layout)
        
        # === 2. Page Stack ===
        self.pages = QStackedWidget()
        
        self.page_control = ControlPage(engine)
        self.page_acq = AcquisitionPage(engine)
        self.page_himc = HiMcPage(engine)

        self.pages.addWidget(self.page_control)
        self.pages.addWidget(self.page_acq)
        self.pages.addWidget(self.page_himc)
        
        main_layout.addWidget(self.pages)
        
        # === 3. Left Dock: Status Panel ===
        self.create_status_dock()
        
        # === 4. Right Dock: Parameters Panel ===
        self.create_parameters_dock()
        
        # === 5. Right Dock: Images and Documents Panel ===
        self.create_images_dock()
        
        # Apply Stylesheet
        self.apply_styles()
        
        # Connect Engine Signals
        self.connect_engine_signals()

    def create_top_bar(self, parent_layout):
        """Create top navigation bar"""
        top_bar = QWidget()
        top_bar.setFixedHeight(50)
        top_bar.setObjectName("TopBar")
        
        bar_layout = QHBoxLayout(top_bar)
        bar_layout.setContentsMargins(20, 0, 20, 0)
        bar_layout.setSpacing(0)
        
        # Logo / Title
        title = QLabel("MultiSEM")
        title.setObjectName("LogoLabel")
        bar_layout.addWidget(title)
        
        # Spacer
        bar_layout.addSpacing(30)
        
        # Tab Buttons
        self.tab_control = QPushButton("Control & Map")
        self.tab_acq = QPushButton("Acquisition")
        self.tab_himc = QPushButton("HI-MC")

        self.tabs = [self.tab_control, self.tab_acq, self.tab_himc]

        for i, tab in enumerate(self.tabs):
            tab.setCheckable(True)
            tab.setObjectName("NavTab")
            bar_layout.addWidget(tab)

        # Connect after loop to avoid lambda capture issue
        self.tab_control.clicked.connect(lambda: self.switch_page(0))
        self.tab_acq.clicked.connect(lambda: self.switch_page(1))
        self.tab_himc.clicked.connect(lambda: self.switch_page(2))
            
        bar_layout.addStretch()
        
        # Select first tab
        self.tab_control.setChecked(True)
        
        parent_layout.addWidget(top_bar)

    def create_status_dock(self):
        """Create left-side status panel"""
        dock = QDockWidget("System Status", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        dock.setMinimumWidth(220)
        dock.setMaximumWidth(300)
        
        self.status_panel = StatusPanel()
        dock.setWidget(self.status_panel)
        
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        
        # Connect status panel signals
        self.status_panel.connect_requested.connect(self.on_connect_requested)
        self.status_panel.disconnect_requested.connect(self.on_disconnect_requested)

    def create_parameters_dock(self):
        """Create right-side floating parameters panel"""
        self.params_dock = QDockWidget("Parameters", self)
        self.params_dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.params_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.params_dock.setMinimumWidth(320)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        
        self.params_panel = ParametersPanel()
        scroll.setWidget(self.params_panel)
        
        self.params_dock.setWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, self.params_dock)
        
        # Connect parameters update signal to acquisition page
        self.params_panel.parameters_updated.connect(self.page_acq.update_from_params_panel)
        
        # Connect regions loaded signal to acquisition page
        self.params_panel.regions_loaded.connect(self.page_acq.load_regions_from_json)
        # Remember the imaging-JSON path on the acquisition page — focus_map.py
        # (run as a subprocess) reads its regions/contours straight from it.
        # Auto-fill both the Acquisition and HI-MC pages' imaging-JSON from the
        # shared Load JSON. Guard against a STALE parameters_panel.py (a partial
        # deploy missing this signal) so it degrades to a warning instead of
        # crashing the whole window at startup.
        if hasattr(self.params_panel, "imaging_json_selected"):
            self.params_panel.imaging_json_selected.connect(self.page_acq.set_imaging_json_path)
            self.params_panel.imaging_json_selected.connect(self.page_himc.set_imaging_json_path)
        else:
            print("[main_window] WARNING: ParametersPanel has no "
                  "'imaging_json_selected' signal — your parameters_panel.py is "
                  "STALE. Re-deploy the full msem_control/ folder so the shared "
                  "Load JSON auto-fills the acquisition pages.")
    
    def create_images_dock(self):
        """Create right-side floating images and documents panel"""
        self.images_dock = QDockWidget("Images && Documents", self)
        self.images_dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.images_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.images_dock.setMinimumWidth(250)
        self.images_dock.setMaximumWidth(350)
        
        self.images_panel = ImagesPanel()
        self.images_dock.setWidget(self.images_panel)
        
        # Tab with Parameters dock (stacked behind it)
        self.addDockWidget(Qt.RightDockWidgetArea, self.images_dock)
        self.tabifyDockWidget(self.params_dock, self.images_dock)
        
        # Start with Parameters visible
        self.params_dock.raise_()

    def apply_styles(self):
        """Apply global stylesheet"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            
            #TopBar {
                background-color: #252526;
                border-bottom: 1px solid #3e3e3e;
            }
            
            #LogoLabel {
                font-size: 16px;
                font-weight: bold;
                color: #569cd6;
            }
            
            #NavTab {
                background-color: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                color: #888888;
                padding: 0 20px;
                font-size: 14px;
                min-height: 40px;
            }
            #NavTab:hover {
                color: #cccccc;
                background-color: rgba(255, 255, 255, 0.05);
            }
            #NavTab:checked {
                color: #ffffff;
                background-color: rgba(0, 122, 204, 0.15);
                border-bottom: 2px solid #007acc;
            }
            #NavTab:checked:hover {
                background-color: rgba(0, 122, 204, 0.25);
            }
            
            QDockWidget {
                color: #cccccc;
                background-color: #1e1e1e;
                titlebar-close-icon: url(none);
            }
            QDockWidget::title {
                background-color: #2d2d2d;
                padding: 8px;
                font-weight: bold;
                color: #cccccc;
            }
            QDockWidget > QWidget {
                background-color: #1e1e1e;
            }
            
            QGroupBox {
                font-weight: bold;
                border: 1px solid #3e3e3e;
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            
            QPushButton {
                background-color: #0e639c;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 4px;
                min-height: 24px;
            }
            QPushButton:hover {
                background-color: #1177bb;
            }
            QPushButton:pressed {
                background-color: #094771;
            }
            QPushButton:disabled {
                background-color: #3e3e3e;
                color: #666666;
            }
            
            QLabel {
                color: #cccccc;
            }
            
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e3e;
                padding: 4px;
                border-radius: 3px;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #007acc;
            }
            
            QScrollArea {
                background-color: #1e1e1e;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #1e1e1e;
            }
            QWidget {
                background-color: #1e1e1e;
            }
            
            QProgressBar {
                border: 1px solid #3e3e3e;
                border-radius: 3px;
                text-align: center;
                background-color: #2d2d2d;
            }
            QProgressBar::chunk {
                background-color: #4ec9b0;
            }
        """)

    def connect_engine_signals(self):
        """Connect workflow engine signals to UI updates"""
        self.engine.on_log = self.handle_log
        self.engine.on_status = self.handle_status
        self.engine.on_position = self.handle_position
        self.engine.on_connected = self.handle_connected
        self.engine.on_operation = self.handle_operation
        self.engine.on_all_status = self.handle_all_status

    def switch_page(self, index):
        for i, tab in enumerate(self.tabs):
            tab.setChecked(i == index)
        self.pages.setCurrentIndex(index)
        
    # --- Signal Handlers ---
    
    def on_connect_requested(self):
        # Check simulation mode from checkbox
        is_simulation = self.status_panel.chk_simulation.isChecked()
        self.engine.simulation_mode = is_simulation
        
        self.status_panel.set_operation("connecting")
        self.engine.connect()
        
    def on_disconnect_requested(self):
        self.engine.disconnect()
        
    def handle_log(self, msg, level="INFO"):
        print(f"[{level}] {msg}")
        
    def handle_status(self, status):
        pass

    def handle_connected(self, connected: bool):
        self.status_panel.set_connected(connected)
        if connected:
            self.status_panel.set_operation("idle")
        else:
            self.status_panel.set_operation("idle")
            
    def handle_position(self, x, y, z):
        # Emit signal for thread-safe update
        self.position_changed.emit(x, y, z)
        
    def handle_operation(self, status: str, progress: int = -1):
        # Emit signal for thread-safe update
        self.operation_changed.emit(status, progress)
        
    def _handle_position_slot(self, x: float, y: float, z: float):
        """Slot for thread-safe position update"""
        self.status_panel.set_position(x, y, z)
        self.page_control.update_position(x, y, z)
    
    def _handle_operation_slot(self, status: str, progress: int):
        """Slot for thread-safe operation status update"""
        self.status_panel.set_operation(status, progress)
        
    def handle_all_status(self, status_dict: dict):
        """Handle comprehensive status update from hardware.

        This runs on the engine's daemon POLL THREAD, so it must NOT touch Qt
        widgets directly — that's a cross-thread GUI access (undefined behaviour,
        a real source of lag/instability over a long real-mode run). Marshal it
        to the main thread via a queued signal, like position/operation do."""
        self.all_status_changed.emit(status_dict)

    def _handle_all_status_slot(self, status_dict: dict):
        """Slot for thread-safe comprehensive status update (main thread)."""
        self.status_panel.update_all_status(status_dict)
