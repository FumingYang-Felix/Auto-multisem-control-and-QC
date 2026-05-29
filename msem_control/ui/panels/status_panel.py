from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QProgressBar, QFrame, QGridLayout, QCheckBox,
    QScrollArea
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor


class StatusLight(QFrame):
    """A simple colored status indicator light"""
    def __init__(self, size=12, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._size = size
        self._current_color = '#555555'
        self._update_style()
        
    def set_color(self, color: str):
        """Set color: 'green', 'yellow', 'red', 'gray', 'blue'"""
        colors = {
            'green': '#4ec9b0',
            'yellow': '#cca700',
            'red': '#f44747',
            'gray': '#555555',
            'blue': '#569cd6',
            'orange': '#ce9178'
        }
        new_color = colors.get(color, '#555555')
        if new_color != self._current_color:
            self._current_color = new_color
            self._update_style()
    
    def _update_style(self):
        """Update the widget style"""
        r = self._size // 2
        style = f"QFrame {{ border-radius: {r}px; background-color: {self._current_color}; border: none; }}"
        self.setStyleSheet(style)


class StatusRow(QWidget):
    """A row with label, light indicator, and value"""
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)
        
        self.light = StatusLight(10)
        self.name_label = QLabel(label)
        self.name_label.setStyleSheet("color: #888; font-size: 11px;")
        self.value_label = QLabel("---")
        self.value_label.setStyleSheet("color: #ccc; font-size: 11px; font-family: monospace;")
        
        layout.addWidget(self.light)
        layout.addWidget(self.name_label)
        layout.addStretch()
        layout.addWidget(self.value_label)
        
        self._last_value = "---"
        self._last_status = 'gray'
        
    def set_value(self, value: str, status: str = 'gray'):
        """Set value and status color (only updates if changed)"""
        if value != self._last_value:
            self.value_label.setText(value)
            self._last_value = value
        if status != self._last_status:
            self.light.set_color(status)
            self._last_status = status


class StatusPanel(QWidget):
    """
    Comprehensive System Status Panel
    Shows all MSEM status parameters from the API
    """
    
    connect_requested = Signal()
    disconnect_requested = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        
    def setup_ui(self):
        # Use scroll area for many parameters
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        
        # === Connection Section ===
        conn_group = QGroupBox("Connection")
        conn_layout = QVBoxLayout(conn_group)
        
        # Simulation mode checkbox
        self.chk_simulation = QCheckBox("Simulation Mode")
        self.chk_simulation.setChecked(True)
        self.chk_simulation.setStyleSheet("color: #cca700; font-weight: bold;")
        conn_layout.addWidget(self.chk_simulation)
        
        # Hardware connection status
        self.row_hw_connected = StatusRow("Hardware")
        conn_layout.addWidget(self.row_hw_connected)
        
        # Connect/Disconnect buttons
        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_requested.emit)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.disconnect_requested.emit)
        self.btn_disconnect.setEnabled(False)
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        conn_layout.addLayout(btn_row)
        
        layout.addWidget(conn_group)
        
        # === Operation Status ===
        op_group = QGroupBox("Current Operation")
        op_layout = QVBoxLayout(op_group)
        
        op_row = QHBoxLayout()
        self.op_light = StatusLight(16)
        self.op_label = QLabel("Idle")
        self.op_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #4ec9b0;")
        op_row.addWidget(self.op_light)
        op_row.addWidget(self.op_label)
        op_row.addStretch()
        op_layout.addLayout(op_row)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(8)
        op_layout.addWidget(self.progress_bar)
        
        layout.addWidget(op_group)
        
        # === Stage Position ===
        pos_group = QGroupBox("Stage Position")
        pos_layout = QVBoxLayout(pos_group)
        
        self.row_stage_x = StatusRow("X")
        self.row_stage_y = StatusRow("Y")
        self.row_stage_z = StatusRow("Z")
        self.row_stage_moving = StatusRow("Moving")
        
        pos_layout.addWidget(self.row_stage_x)
        pos_layout.addWidget(self.row_stage_y)
        pos_layout.addWidget(self.row_stage_z)
        pos_layout.addWidget(self.row_stage_moving)
        
        layout.addWidget(pos_group)
        
        # === Beam Status ===
        beam_group = QGroupBox("Beam")
        beam_layout = QVBoxLayout(beam_group)
        
        self.row_beam_on = StatusRow("Beam")
        self.row_beam_lock = StatusRow("Beam Lock")
        self.row_emitter = StatusRow("Emitter Current")
        
        beam_layout.addWidget(self.row_beam_on)
        beam_layout.addWidget(self.row_beam_lock)
        beam_layout.addWidget(self.row_emitter)
        
        layout.addWidget(beam_group)
        
        # === Vacuum Status ===
        vac_group = QGroupBox("Vacuum")
        vac_layout = QVBoxLayout(vac_group)
        
        self.row_vac_status = StatusRow("Status")
        self.row_p_chamber = StatusRow("Chamber")
        self.row_p_gun = StatusRow("Gun")
        self.row_p_column = StatusRow("Upper Column")
        self.row_p_optics = StatusRow("Micro-Optics")
        self.row_p_scint = StatusRow("Scintillator")
        
        vac_layout.addWidget(self.row_vac_status)
        vac_layout.addWidget(self.row_p_chamber)
        vac_layout.addWidget(self.row_p_gun)
        vac_layout.addWidget(self.row_p_column)
        vac_layout.addWidget(self.row_p_optics)
        vac_layout.addWidget(self.row_p_scint)
        
        layout.addWidget(vac_group)
        
        # === E-Chuck Status ===
        echuck_group = QGroupBox("E-Chuck")
        echuck_layout = QVBoxLayout(echuck_group)
        
        self.row_echuck_on = StatusRow("Status")
        self.row_echuck_voltage = StatusRow("Voltage")
        self.row_echuck_current = StatusRow("Current")
        
        echuck_layout.addWidget(self.row_echuck_on)
        echuck_layout.addWidget(self.row_echuck_voltage)
        echuck_layout.addWidget(self.row_echuck_current)
        
        layout.addWidget(echuck_group)
        
        layout.addStretch()
        
        scroll.setWidget(content)
        
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)
        
    # === Public API ===
    
    def set_connected(self, connected: bool):
        if connected:
            self.row_hw_connected.set_value("Connected", "green")
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
            self.chk_simulation.setEnabled(False)
        else:
            self.row_hw_connected.set_value("Disconnected", "gray")
            self.btn_connect.setEnabled(True)
            self.btn_disconnect.setEnabled(False)
            self.chk_simulation.setEnabled(True)
            
    def set_operation(self, status: str, progress: int = -1):
        """Set current operation status"""
        status_info = {
            'idle': ('Idle', 'green'),
            'connecting': ('Connecting...', 'yellow'),
            'moving': ('Moving Stage', 'yellow'),
            'acquiring': ('Acquiring', 'blue'),
            'autofocusing': ('Autofocusing', 'blue'),
            'autostig': ('Auto Stigmation', 'blue'),
            'processing': ('Processing', 'yellow'),
            'error': ('Error', 'red')
        }
        
        text, color = status_info.get(status.lower(), (status, 'gray'))
        self.op_label.setText(text)
        self.op_light.set_color(color)
        
        # Update label color
        color_map = {'green': '#4ec9b0', 'yellow': '#cca700', 'blue': '#569cd6', 'red': '#f44747'}
        self.op_label.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {color_map.get(color, '#ccc')};")
        
        if progress >= 0:
            self.progress_bar.setValue(progress)
            self.progress_bar.setVisible(True)
        else:
            self.progress_bar.setVisible(False)
            
    def set_position(self, x: float, y: float, z: float):
        self.row_stage_x.set_value(f"{x:,.1f} µm", "green")
        self.row_stage_y.set_value(f"{y:,.1f} µm", "green")
        self.row_stage_z.set_value(f"{z:,.1f} µm", "green")
        
    def set_stage_moving(self, is_moving: bool):
        if is_moving:
            self.row_stage_moving.set_value("YES", "yellow")
        else:
            self.row_stage_moving.set_value("No", "green")
        
    def set_beam_status(self, is_on: bool, is_locked: bool = False, emitter_current: float = 0):
        if is_on:
            self.row_beam_on.set_value("ON", "green")
        else:
            self.row_beam_on.set_value("OFF", "red")
            
        if is_locked:
            self.row_beam_lock.set_value("Locked", "green")
        else:
            self.row_beam_lock.set_value("Unlocked", "gray")
            
        self.row_emitter.set_value(f"{emitter_current:.2f} µA", "green" if emitter_current > 0 else "gray")
        
    def set_vacuum_status(self, status: int, pressures: dict = None):
        """
        Set vacuum status
        status: 0=Not ready, 1=Ready (from DP_VACSTATUS)
        pressures: dict with keys 'chamber', 'gun', 'column', 'optics', 'scintillator'
        """
        if status == 1:
            self.row_vac_status.set_value("Ready", "green")
        else:
            self.row_vac_status.set_value("Not Ready", "yellow")
            
        if pressures:
            def format_pressure(p):
                if p < 1e-6:
                    return f"{p*1e9:.1f}e-9 mbar"
                elif p < 1e-3:
                    return f"{p*1e6:.1f}e-6 mbar"
                else:
                    return f"{p:.2e} mbar"
                    
            if 'chamber' in pressures:
                self.row_p_chamber.set_value(format_pressure(pressures['chamber']), "green")
            if 'gun' in pressures:
                self.row_p_gun.set_value(format_pressure(pressures['gun']), "green")
            if 'column' in pressures:
                self.row_p_column.set_value(format_pressure(pressures['column']), "green")
            if 'optics' in pressures:
                self.row_p_optics.set_value(format_pressure(pressures['optics']), "green")
            if 'scintillator' in pressures:
                self.row_p_scint.set_value(format_pressure(pressures['scintillator']), "green")
                
    def set_echuck_status(self, is_on: bool, voltage: float = 0, current: float = 0):
        if is_on:
            self.row_echuck_on.set_value("ON", "green")
        else:
            self.row_echuck_on.set_value("OFF", "gray")
            
        self.row_echuck_voltage.set_value(f"{voltage:.1f} V", "green" if voltage > 0 else "gray")
        self.row_echuck_current.set_value(f"{current:.2f} nA", "green" if is_on else "gray")
        
    def update_all_status(self, status_dict: dict):
        """
        Update all status from a dictionary
        Expected keys from MSEM API:
        - DP_IS_HARDWARE_CONNECTED
        - DP_IS_BEAM_ON
        - DP_IS_BEAMLOCK_ON
        - DP_IS_STAGE_MOVING
        - DP_VACSTATUS
        - DP_IS_ECHUCK_ON
        - AP_STAGE_AT_X/Y/Z
        - AP_EMITTER_CURRENT
        - AP_PRESSURE_CHAMBER/GUN/UPPERCOLUMN/MICROOPTICS/SCINTILLATOR
        - AP_ECHUCK_VOLTAGE_MONITOR
        - AP_ECHUCK_CURRENT_MONITOR
        """
        # Hardware connection
        if 'DP_IS_HARDWARE_CONNECTED' in status_dict:
            self.set_connected(status_dict['DP_IS_HARDWARE_CONNECTED'] == 1)
            
        # Position
        if 'AP_STAGE_AT_X' in status_dict:
            self.set_position(
                status_dict.get('AP_STAGE_AT_X', 0),
                status_dict.get('AP_STAGE_AT_Y', 0),
                status_dict.get('AP_STAGE_AT_Z', 0)
            )
            
        # Stage moving
        if 'DP_IS_STAGE_MOVING' in status_dict:
            self.set_stage_moving(status_dict['DP_IS_STAGE_MOVING'] == 1)
            
        # Beam
        if 'DP_IS_BEAM_ON' in status_dict:
            self.set_beam_status(
                status_dict.get('DP_IS_BEAM_ON', 0) == 1,
                status_dict.get('DP_IS_BEAMLOCK_ON', 0) == 1,
                status_dict.get('AP_EMITTER_CURRENT', 0)
            )
            
        # Vacuum
        if 'DP_VACSTATUS' in status_dict:
            pressures = {
                'chamber': status_dict.get('AP_PRESSURE_CHAMBER', 0),
                'gun': status_dict.get('AP_PRESSURE_GUN', 0),
                'column': status_dict.get('AP_PRESSURE_UPPERCOLUMN', 0),
                'optics': status_dict.get('AP_PRESSURE_MICROOPTICS', 0),
                'scintillator': status_dict.get('AP_PRESSURE_SCINTILLATOR', 0)
            }
            self.set_vacuum_status(status_dict['DP_VACSTATUS'], pressures)
            
        # E-Chuck
        if 'DP_IS_ECHUCK_ON' in status_dict:
            self.set_echuck_status(
                status_dict.get('DP_IS_ECHUCK_ON', 0) == 1,
                status_dict.get('AP_ECHUCK_VOLTAGE_MONITOR', 0),
                status_dict.get('AP_ECHUCK_CURRENT_MONITOR', 0)
            )
