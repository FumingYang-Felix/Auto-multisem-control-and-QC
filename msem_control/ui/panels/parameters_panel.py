from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
    QScrollArea, QFormLayout, QFileDialog, QDoubleSpinBox,
    QLineEdit, QSpinBox, QComboBox, QListWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QCheckBox
)
from PySide6.QtCore import Qt, Signal
import json
from ..widgets import CollapsibleBox

class ParametersPanel(QWidget):
    """Floating parameters panel - always visible on the right side"""
    
    # Signal emitted when user clicks "Update" button
    parameters_updated = Signal(dict)  # Emits all parameter values
    
    # Signal emitted when regions are loaded from JSON
    regions_loaded = Signal(list)  # Emits list of (region_name, polygon_points, support_points)

    # Signal emitted with the imaging-JSON file path when a Zeiss experiment
    # JSON is opened. The acquisition pipeline (focus_map.py) reads its regions
    # + contours straight from this file, so the GUI must remember the path.
    imaging_json_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded_json_path = None
        self.setup_ui()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignTop)
        
        # Toolbar: Load/Save Config
        toolbar = QHBoxLayout()
        btn_load = QPushButton("📂 Load JSON")
        btn_load.clicked.connect(self.load_config)
        btn_save = QPushButton("💾 Save JSON")
        toolbar.addWidget(btn_load)
        toolbar.addWidget(btn_save)
        layout.addLayout(toolbar)
        
        # Update Button - sync parameters to left panels
        btn_update = QPushButton("🔄 Update All Parameters")
        btn_update.setStyleSheet("""
            QPushButton {
                background-color: #0e639c;
                font-weight: bold;
                padding: 8px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #1177bb;
            }
        """)
        btn_update.clicked.connect(self.update_parameters)
        layout.addWidget(btn_update)
        
        # Info label
        update_info = QLabel("Edit parameters above, then click Update to apply globally")
        update_info.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
        update_info.setWordWrap(True)
        layout.addWidget(update_info)
        
        # === Parameter Groups ===
        
        # 1. Metadata
        self.create_metadata_group(layout)
        
        # 2. Beam Settings
        self.create_beam_group(layout)
        
        # 3. Stage Settings
        self.create_stage_group(layout)
        
        # 4. Scan Settings
        self.create_scan_group(layout)
        
        # 5. ROI Settings
        self.create_roi_group(layout)
        
        # 6. Output Settings
        self.create_output_group(layout)
        
        # 7. Autofocus Settings
        self.create_autofocus_group(layout)
        
        # 8. Autostig Settings
        self.create_autostig_group(layout)
        
        # 9. Regions (loaded from JSON)
        self.create_regions_group(layout)
        
        layout.addStretch()

    def create_metadata_group(self, parent):
        box = CollapsibleBox("Metadata")
        form = QFormLayout()
        self.edit_name = QLineEdit()
        self.edit_operator = QLineEdit()
        form.addRow("Experiment:", self.edit_name)
        form.addRow("Operator:", self.edit_operator)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_beam_group(self, parent):
        box = CollapsibleBox("Beam Settings")
        form = QFormLayout()
        self.spin_voltage = QDoubleSpinBox()
        self.spin_voltage.setRange(0, 30)
        self.spin_voltage.setValue(1.5)
        self.spin_voltage.setSuffix(" kV")
        self.spin_current = QSpinBox()
        self.spin_current.setRange(0, 10000)
        self.spin_current.setValue(500)
        self.spin_current.setSuffix(" pA")
        self.spin_aperture = QSpinBox()
        self.spin_aperture.setRange(0, 100)
        self.spin_aperture.setValue(30)
        self.spin_aperture.setSuffix(" µm")
        form.addRow("Voltage:", self.spin_voltage)
        form.addRow("Current:", self.spin_current)
        form.addRow("Aperture:", self.spin_aperture)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_stage_group(self, parent):
        box = CollapsibleBox("Stage Settings")
        form = QFormLayout()
        self.spin_wd = QDoubleSpinBox()
        self.spin_wd.setRange(0, 50000)
        self.spin_wd.setValue(38500)
        self.spin_wd.setSuffix(" µm")
        form.addRow("Working Distance:", self.spin_wd)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_scan_group(self, parent):
        box = CollapsibleBox("Scan Settings")
        form = QFormLayout()
        
        # AP_PIXELSIZE: typical range 1-100 nm (from API examples: 4-10 nm typical)
        self.spin_pixel_size = QDoubleSpinBox()
        self.spin_pixel_size.setRange(1.0, 100.0)
        self.spin_pixel_size.setValue(4.0)
        self.spin_pixel_size.setSuffix(" nm")
        self.spin_pixel_size.setDecimals(1)
        
        # Dwell time: typical range
        self.spin_dwell = QSpinBox()
        self.spin_dwell.setRange(1, 10000)
        self.spin_dwell.setValue(100)
        self.spin_dwell.setSuffix(" ns")
        
        # Scan rate: 1-8 typical (from test scripts)
        self.spin_scanrate = QSpinBox()
        self.spin_scanrate.setRange(1, 8)
        self.spin_scanrate.setValue(1)
        
        form.addRow("Pixel Size:", self.spin_pixel_size)
        form.addRow("Dwell Time:", self.spin_dwell)
        form.addRow("Scan Rate:", self.spin_scanrate)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_roi_group(self, parent):
        box = CollapsibleBox("ROI Settings")
        form = QFormLayout()
        
        # AP_HEXAGON_OVERLAP: 0.0 - 0.5 (0-50% of MFOV)
        self.spin_overlap_mfov = QDoubleSpinBox()
        self.spin_overlap_mfov.setRange(0.0, 50.0)
        self.spin_overlap_mfov.setValue(4.0)
        self.spin_overlap_mfov.setSuffix(" %")
        self.spin_overlap_mfov.setDecimals(2)
        
        # AP_OVERLAP_X/Y: 0 - 6 µm (from API guidebook)
        self.spin_overlap_sfov = QDoubleSpinBox()
        self.spin_overlap_sfov.setRange(0.0, 6.0)
        self.spin_overlap_sfov.setValue(1.0)
        self.spin_overlap_sfov.setSuffix(" µm")
        self.spin_overlap_sfov.setDecimals(2)
        
        self.combo_travel = QComboBox()
        self.combo_travel.addItems(["comb", "snake"])
        
        self.combo_direction = QComboBox()
        self.combo_direction.addItems(["horizontal_up", "horizontal_down", "vertical_left", "vertical_right"])
        
        form.addRow("Overlap MFOV:", self.spin_overlap_mfov)
        form.addRow("Overlap SFOV:", self.spin_overlap_sfov)
        form.addRow("Travel:", self.combo_travel)
        form.addRow("Direction:", self.combo_direction)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_output_group(self, parent):
        box = CollapsibleBox("Output Settings")
        form = QFormLayout()
        
        # Output folder with browse button
        folder_layout = QHBoxLayout()
        self.edit_out_folder = QLineEdit("V:\\Fuming\\test")
        btn_browse = QPushButton("...")
        btn_browse.setFixedWidth(30)
        btn_browse.clicked.connect(self.browse_output_folder)
        folder_layout.addWidget(self.edit_out_folder)
        folder_layout.addWidget(btn_browse)
        
        self.edit_prefix = QLineEdit("sample")
        
        # Image settings
        self.chk_save_images = QCheckBox()
        self.chk_save_images.setChecked(True)
        self.combo_format = QComboBox()
        self.combo_format.addItems(["bmp", "tif", "png"])
        
        # Thumbnail settings
        self.chk_save_thumbnails = QCheckBox()
        self.chk_save_thumbnails.setChecked(True)
        self.combo_thumbnail_format = QComboBox()
        self.combo_thumbnail_format.addItems(["jpg", "bmp", "png"])
        self.spin_thumb_scale = QSpinBox()
        self.spin_thumb_scale.setRange(1, 16)
        self.spin_thumb_scale.setValue(4)
        self.spin_thumb_scale.setToolTip("Thumbnail downsampling factor")
        
        form.addRow("Folder:", folder_layout)
        form.addRow("Prefix:", self.edit_prefix)
        form.addRow("Save Images:", self.chk_save_images)
        form.addRow("Image Format:", self.combo_format)
        form.addRow("Save Thumbnails:", self.chk_save_thumbnails)
        form.addRow("Thumbnail Format:", self.combo_thumbnail_format)
        form.addRow("Thumbnail Scale:", self.spin_thumb_scale)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_autofocus_group(self, parent):
        box = CollapsibleBox("Autofocus Settings")
        form = QFormLayout()
        
        # AP_AUTOFOCUS_RANGE: 10 - 10000 µm (from API guidebook)
        self.spin_af_range = QDoubleSpinBox()
        self.spin_af_range.setRange(10.0, 10000.0)
        self.spin_af_range.setValue(150.0)
        self.spin_af_range.setSuffix(" µm")
        self.spin_af_range.setDecimals(1)
        
        # AP_AUTOFOCUS_PIXELSIZE: 1 - 25 nm (from API guidebook)
        self.spin_af_pixel = QDoubleSpinBox()
        self.spin_af_pixel.setRange(1.0, 25.0)
        self.spin_af_pixel.setValue(4.0)
        self.spin_af_pixel.setSuffix(" nm")
        self.spin_af_scanrate = QSpinBox()
        self.spin_af_scanrate.setRange(1, 20)
        self.spin_af_scanrate.setValue(1)
        form.addRow("Range:", self.spin_af_range)
        form.addRow("Pixel Size:", self.spin_af_pixel)
        form.addRow("Scan Rate:", self.spin_af_scanrate)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_autostig_group(self, parent):
        box = CollapsibleBox("Autostig Settings")
        form = QFormLayout()
        self.spin_stig_range = QDoubleSpinBox()
        self.spin_stig_range.setRange(0, 10)
        self.spin_stig_range.setValue(2.0)
        self.spin_stig_pixel = QDoubleSpinBox()
        self.spin_stig_pixel.setRange(1, 100)
        self.spin_stig_pixel.setValue(4.0)
        self.spin_stig_pixel.setSuffix(" nm")
        self.spin_stig_scanrate = QSpinBox()
        self.spin_stig_scanrate.setRange(1, 20)
        self.spin_stig_scanrate.setValue(2)
        form.addRow("Range:", self.spin_stig_range)
        form.addRow("Pixel Size:", self.spin_stig_pixel)
        form.addRow("Scan Rate:", self.spin_stig_scanrate)
        box.set_content_layout(form)
        parent.addWidget(box)

    def create_regions_group(self, parent):
        """Create a collapsible section to display loaded regions"""
        box = CollapsibleBox("Regions (from JSON)")
        content = QVBoxLayout()
        
        # Info label
        info_label = QLabel("Load a JSON file to display regions")
        info_label.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
        info_label.setWordWrap(True)
        content.addWidget(info_label)
        
        # Regions list
        self.regions_list = QListWidget()
        self.regions_list.setMaximumHeight(150)
        self.regions_list.setStyleSheet("""
            QListWidget {
                background-color: #2d2d2d;
                border: 1px solid #555;
                color: #cccccc;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
        """)
        self.regions_list.itemDoubleClicked.connect(self._on_region_double_clicked)
        content.addWidget(self.regions_list)
        
        # Apply button - load selected regions into Acquisition Page
        self.btn_apply_regions = QPushButton("📥 Load Regions to Acquisition Page")
        self.btn_apply_regions.setToolTip("Import all regions and support points to Acquisition Page")
        self.btn_apply_regions.clicked.connect(self._on_apply_regions)
        self.btn_apply_regions.setEnabled(False)  # Disabled until JSON is loaded
        content.addWidget(self.btn_apply_regions)
        
        # Store loaded experiment data
        self._loaded_experiment = None
        
        box.set_content_layout(content)
        parent.addWidget(box)

    # --- Actions ---
    
    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.edit_out_folder.setText(folder)
    
    def update_parameters(self):
        """Collect all parameters and emit signal to update left panels"""
        params = {
            # Metadata
            'experiment_name': self.edit_name.text(),
            'operator': self.edit_operator.text(),
            
            # Beam Settings
            'beam_voltage': self.spin_voltage.value(),
            'beam_current': self.spin_current.value(),
            'aperture': self.spin_aperture.value(),
            
            # Stage Settings
            'working_distance': self.spin_wd.value(),
            
            # Scan Settings
            'pixel_size': self.spin_pixel_size.value(),
            'dwell_time': self.spin_dwell.value(),
            'scan_rate': self.spin_scanrate.value(),
            
            # ROI Settings
            'mfov_overlap': self.spin_overlap_mfov.value(),
            'sfov_overlap': self.spin_overlap_sfov.value(),
            'travel_pattern': self.combo_travel.currentText(),
            'travel_direction': self.combo_direction.currentText(),
            
            # Output Settings
            'output_folder': self.edit_out_folder.text(),
            'output_prefix': self.edit_prefix.text(),
            'save_images': self.chk_save_images.isChecked(),
            'output_format': self.combo_format.currentText(),
            'save_thumbnails': self.chk_save_thumbnails.isChecked(),
            'thumbnail_format': self.combo_thumbnail_format.currentText(),
            'thumbnail_scale': self.spin_thumb_scale.value(),
            
            # Autofocus Settings
            'af_range': self.spin_af_range.value(),
            'af_pixel_size': self.spin_af_pixel.value(),
        }
        
        print(f"[Parameters Panel] Emitting parameters_updated signal with {len(params)} parameters")
        print(f"  pixel_size={params['pixel_size']}nm, mfov_overlap={params['mfov_overlap']}%, "
              f"scan_rate={params['scan_rate']}, output_folder={params['output_folder']}")
        
        # Emit signal with all parameters
        self.parameters_updated.emit(params)
        
        print(f"[Parameters Panel] Signal emitted successfully")

    def load_config(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Config", "", "JSON Files (*.json)")
        if fname:
            try:
                with open(fname, 'r') as f:
                    data = json.load(f)
                # Remember the path + tell listeners IF this is a Zeiss imaging
                # JSON (the kind focus_map.py consumes). Custom GUI configs don't
                # count as an imaging JSON.
                if "ExperimentParameter" in data:
                    self._loaded_json_path = fname
                    self.imaging_json_selected.emit(fname)
                self.apply_config(data)
            except Exception as e:
                print(f"Error loading config: {e}")

    def apply_config(self, data):
        """
        Parse JSON and update UI fields.
        Supports both custom format and ZEISS experiment format.
        """
        try:
            # Check if it's ZEISS experiment format
            if "ExperimentParameter" in data:
                self._apply_zeiss_format(data)
            else:
                self._apply_custom_format(data)
        except Exception as e:
            print(f"Error applying config: {e}")
            import traceback
            traceback.print_exc()
            
    def _apply_zeiss_format(self, data):
        """Parse ZEISS MultiSEM experiment JSON format"""
        exp = data.get("ExperimentParameter", {})
        
        # === Metadata ===
        self.edit_name.setText(data.get("Name", exp.get("ExperimentName", "")))
        self.edit_operator.setText(exp.get("ExpOperator", ""))
        
        # === Output Settings ===
        exp_path = exp.get("ExperimentPath", "")
        if exp_path:
            self.edit_out_folder.setText(exp_path)
            
        # === Stage Settings ===
        settling_time = exp.get("StageSettlingTime", 0.8)
        # Could add a field for this if needed
        
        # === Print summary ===
        regions = data.get("Regions", [])
        images = exp.get("Images", [])
        
        print(f"═══════════════════════════════════════")
        print(f"Loaded ZEISS Experiment: {data.get('Name', 'Unknown')}")
        print(f"═══════════════════════════════════════")
        print(f"  Operator: {exp.get('ExpOperator', 'N/A')}")
        print(f"  Output Path: {exp_path}")
        print(f"  Stage Settling Time: {settling_time} s")
        print(f"  Z Lowering Distance: {exp.get('ZLoweringDistance', 0)} µm")
        print(f"  Optical Images: {len(images)}")
        print(f"  Regions (ROIs): {len(regions)}")
        
        # === Parse Regions for ROI info using zeiss_parser ===
        if regions:
            from ...utils.zeiss_parser import parse_zeiss_dict
            self._loaded_experiment = parse_zeiss_dict(data)
            self._populate_regions_list()
            self.btn_apply_regions.setEnabled(True)
            
            # Extract acquisition parameters from first region's WorkflowParameter
            self._extract_workflow_parameters(regions[0])
            
    def _extract_workflow_parameters(self, region_xml: str):
        """Extract acquisition parameters from WorkflowParameter in region XML"""
        import re
        
        workflow_match = re.search(r'<WorkflowParameter([^>]+)>', region_xml)
        if not workflow_match:
            return
        
        params_str = workflow_match.group(1)
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', params_str))
        
        print(f"\n  Extracting acquisition parameters from WorkflowParameter:")
        
        # === Scan Settings ===
        if 'PixelSize' in attrs:
            pixel_size = float(attrs['PixelSize'])
            self.spin_pixel_size.setValue(pixel_size)
            print(f"    Pixel Size: {pixel_size} nm")
        
        if 'DwellTime' in attrs:
            dwell_time = float(attrs['DwellTime'])
            self.spin_dwell.setValue(dwell_time)
            print(f"    Dwell Time: {dwell_time} ns")
        
        # === ROI Settings ===
        if 'MFoVOverlap' in attrs:
            mfov_overlap = float(attrs['MFoVOverlap'])
            self.spin_overlap_mfov.setValue(mfov_overlap)
            print(f"    MFOV Overlap: {mfov_overlap}%")
        
        if 'SFoVOverlap' in attrs:
            sfov_overlap = float(attrs['SFoVOverlap'])
            self.spin_overlap_sfov.setValue(sfov_overlap)
            print(f"    SFOV Overlap: {sfov_overlap} µm")
        
        if 'TileArrangement' in attrs:
            tile_arrangement = attrs['TileArrangement']
            idx = self.combo_travel.findText(tile_arrangement, Qt.MatchFixedString)
            if idx >= 0:
                self.combo_travel.setCurrentIndex(idx)
            print(f"    Tile Arrangement: {tile_arrangement}")
        
        if 'TileDirectionZEN' in attrs:
            tile_direction = attrs['TileDirectionZEN']
            idx = self.combo_direction.findText(tile_direction, Qt.MatchContains)
            if idx >= 0:
                self.combo_direction.setCurrentIndex(idx)
            print(f"    Tile Direction: {tile_direction}")
        
        # === Output Settings ===
        if 'SaveImages' in attrs:
            save_images = attrs['SaveImages'].lower() == 'true'
            self.chk_save_images.setChecked(save_images)
            print(f"    Save Images: {save_images}")
        
        if 'ImageFormat' in attrs:
            image_format = attrs['ImageFormat']
            idx = self.combo_format.findText(image_format, Qt.MatchFixedString)
            if idx >= 0:
                self.combo_format.setCurrentIndex(idx)
            print(f"    Image Format: {image_format}")
        
        if 'SaveThumbnails' in attrs:
            save_thumbnails = attrs['SaveThumbnails'].lower() == 'true'
            self.chk_save_thumbnails.setChecked(save_thumbnails)
            print(f"    Save Thumbnails: {save_thumbnails}")
        
        if 'ThumbnailFormat' in attrs:
            thumb_format = attrs['ThumbnailFormat']
            idx = self.combo_thumbnail_format.findText(thumb_format, Qt.MatchFixedString)
            if idx >= 0:
                self.combo_thumbnail_format.setCurrentIndex(idx)
            print(f"    Thumbnail Format: {thumb_format}")
        
        if 'ThumbScale' in attrs:
            thumb_scale = int(attrs['ThumbScale'])
            self.spin_thumb_scale.setValue(thumb_scale)
            print(f"    Thumbnail Scale: {thumb_scale}x")
        
        # === Autofocus Settings ===
        if 'AfPixelSize' in attrs:
            af_pixel_size = float(attrs['AfPixelSize'])
            self.spin_af_pixel.setValue(af_pixel_size)
            print(f"    AF Pixel Size: {af_pixel_size} nm")
        
        if 'AfDwellTime' in attrs:
            af_dwell = float(attrs['AfDwellTime'])
            # Note: We don't have an AF dwell time field, could add if needed
            print(f"    AF Dwell Time: {af_dwell} ns")
        
        print(f"  ✓ Parameters loaded successfully")
    
    def _populate_regions_list(self):
        """Populate the regions list widget with loaded regions"""
        self.regions_list.clear()
        
        if not self._loaded_experiment or not self._loaded_experiment.regions:
            return
        
        for i, region in enumerate(self._loaded_experiment.regions):
            # Create list item with region info
            item_text = f"{region.name} - {len(region.polygon_points)} vertices, {len(region.support_points)} AF points"
            self.regions_list.addItem(item_text)
        
        print(f"\n[ParametersPanel] Loaded {len(self._loaded_experiment.regions)} regions into list")
    
    def _on_region_double_clicked(self, item):
        """Show details of a region when double-clicked"""
        idx = self.regions_list.row(item)
        if not self._loaded_experiment or idx >= len(self._loaded_experiment.regions):
            return
        
        region = self._loaded_experiment.regions[idx]
        
        print(f"\n═══ Region Details ═══")
        print(f"Name: {region.name}")
        print(f"Center: ({region.center_x:.1f}, {region.center_y:.1f}) µm")
        print(f"Z: {region.z:.1f} µm")
        print(f"Contour Size: {region.contour_width:.1f} × {region.contour_height:.1f} µm")
        print(f"Polygon Vertices: {len(region.polygon_points)}")
        print(f"Support Points: {len(region.support_points)}")
        if region.support_points:
            print(f"  First AF point: ({region.support_points[0].x:.1f}, {region.support_points[0].y:.1f}, {region.support_points[0].z:.1f}) µm")
    
    def _on_apply_regions(self):
        """Apply all loaded regions to Acquisition Page"""
        if not self._loaded_experiment:
            print("[ParametersPanel] No experiment loaded")
            return
        
        # Prepare data to emit: list of (region_name, polygon_points, support_points_dict)
        regions_data = []
        for region in self._loaded_experiment.regions:
            # Support points as dict with keys: 'x', 'y', 'z', 'ol', etc.
            sp_dicts = [
                {
                    'x': sp.x,
                    'y': sp.y,
                    'z': sp.z,
                    'ol': sp.ol,
                    'ol_tilt_x': sp.ol_tilt_x,
                    'ol_tilt_y': sp.ol_tilt_y
                }
                for sp in region.support_points
            ]
            
            regions_data.append({
                'name': region.name,
                'polygon_points': region.polygon_points,  # List of (x, y) tuples
                'support_points': sp_dicts,
                'z': region.z,
                'center_x': region.center_x,
                'center_y': region.center_y
            })
        
        print(f"[ParametersPanel] Emitting {len(regions_data)} regions to Acquisition Page")
        self.regions_loaded.emit(regions_data)
        
    def _apply_custom_format(self, data):
        """Parse custom config JSON format"""
        # Metadata
        if "metadata" in data:
            meta = data["metadata"]
            self.edit_name.setText(meta.get("name", meta.get("experiment_name", "")))
            self.edit_operator.setText(meta.get("operator", ""))
            
        # Microscope
        if "microscope" in data:
            mic = data["microscope"]
            if "beam" in mic:
                self.spin_voltage.setValue(mic["beam"].get("voltage", 1.5))
                self.spin_current.setValue(mic["beam"].get("current", 500))
                self.spin_aperture.setValue(mic["beam"].get("aperture", 30))
            if "stage" in mic:
                self.spin_wd.setValue(mic["stage"].get("working_distance", 38500))
                
        # Acquisition
        if "acquisition" in data:
            acq = data["acquisition"]
            self.edit_out_folder.setText(acq.get("output_folder", ""))
            self.edit_prefix.setText(acq.get("file_prefix", ""))
            if "scan" in acq:
                self.spin_pixel_size.setValue(acq["scan"].get("pixel_size", 4.0))
                self.spin_dwell.setValue(acq["scan"].get("dwell_time", 100))
                self.spin_scanrate.setValue(acq["scan"].get("scan_rate", 1))
            if "roi" in acq:
                self.spin_overlap_mfov.setValue(acq["roi"].get("overlap_mfov", 0.04))
                self.spin_overlap_sfov.setValue(acq["roi"].get("overlap_sfov", 1.0))
                travel = acq["roi"].get("travel_pattern", "comb")
                idx = self.combo_travel.findText(travel)
                if idx >= 0: self.combo_travel.setCurrentIndex(idx)
                direction = acq["roi"].get("travel_direction", "horizontal_up")
                idx = self.combo_direction.findText(direction)
                if idx >= 0: self.combo_direction.setCurrentIndex(idx)
                
        # Autofocus
        if "autofocus" in data:
            af = data["autofocus"]
            self.spin_af_range.setValue(af.get("range", 150))
            self.spin_af_pixel.setValue(af.get("pixel_size", 4.0))
            self.spin_af_scanrate.setValue(af.get("scan_rate", 1))
            
        # Autostig
        if "autostig" in data:
            stig = data["autostig"]
            self.spin_stig_range.setValue(stig.get("range", 2.0))
            self.spin_stig_pixel.setValue(stig.get("pixel_size", 4.0))
            self.spin_stig_scanrate.setValue(stig.get("scan_rate", 2))

