"""
ROI (Region of Interest) acquisition for MultiSEM

Implements full section/ROI scanning with configurable:
- Outer MFOV overlap (between hexagonal multi-FOV tiles)
- Inner SFOV overlap (between sub-images within MFOV)
- Travel pattern (Comb/Snake)
- Travel direction (horizontal/vertical)
- Start position (Left/Right/Top/Bottom)

API Reference:
- AP_HEXAGON_OVERLAP: 0.0 - 0.5 (relative to MFOV, ~110µm)
- AP_OVERLAP / AP_OVERLAP_X / AP_OVERLAP_Y: 0 - 6 µm
- AP_PIXELSIZE: 1 - 100 nm
- AP_ROI_VERTEX_X, AP_ROI_VERTEX_Y: Define ROI vertices
- CMD_CALCULATE_ROI_VERTICES: Calculate tile positions
- AP_ROI_NUMBEROFPOINTS: Number of generated tiles
- AP_ROI_XPOSITIONS, AP_ROI_YPOSITIONS: Tile coordinates
"""

from __future__ import annotations
from typing import List, Tuple, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

try:
    from .connection import MSEMConnection
    from .stage import Stage
    from .acquisition import Acquisition
except ImportError:
    from connection import MSEMConnection
    from stage import Stage
    from acquisition import Acquisition


class TravelPattern(Enum):
    """Travel pattern between tiles"""
    COMB = "comb"      # Unidirectional rows (like a comb)
    SNAKE = "snake"    # Bidirectional rows (serpentine)


class TravelDirection(Enum):
    """Direction of tile travel"""
    HORIZONTAL_UP = "horizontal_up"       # Rows left-right, moving up
    HORIZONTAL_DOWN = "horizontal_down"   # Rows left-right, moving down
    VERTICAL_LEFT = "vertical_left"       # Columns top-bottom, moving left
    VERTICAL_RIGHT = "vertical_right"     # Columns top-bottom, moving right


class StartPosition(Enum):
    """Starting corner position"""
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"


@dataclass
class ROIConfig:
    """Configuration for ROI acquisition"""
    # Overlap settings
    outer_mfov_overlap: float = 0.04      # 4% of MFOV (~110µm)
    inner_sfov_overlap: float = 1.0       # 1 µm between sub-images
    
    # Image settings
    pixelsize: float = 10.0               # 10 nm pixel size
    image_filetype: str = "bmp"
    thumbnail_filetype: str = "jpg"
    
    # Travel settings
    travel_pattern: TravelPattern = TravelPattern.COMB
    travel_direction: TravelDirection = TravelDirection.HORIZONTAL_UP
    start_position: StartPosition = StartPosition.BOTTOM_LEFT
    
    # Output
    output_folder: str = ""
    experiment_name: str = ""
    sample_name: str = ""
    operator_name: str = ""


@dataclass
class TilePosition:
    """Position of a single tile"""
    x: float      # X coordinate in µm
    y: float      # Y coordinate in µm
    index: int    # Original index from API
    row: int = 0  # Row in grid (for travel ordering)
    col: int = 0  # Column in grid (for travel ordering)


@dataclass
class ROIProgress:
    """Progress information for ROI acquisition"""
    current_tile: int = 0
    total_tiles: int = 0
    current_x: float = 0.0
    current_y: float = 0.0
    percent_complete: float = 0.0


class ROIAcquisition:
    """
    Region of Interest (ROI) acquisition interface.
    
    Usage:
        conn = MSEMConnection()
        conn.connect_and_initialize()
        
        roi_acq = ROIAcquisition(conn)
        
        # Configure with defaults
        config = ROIConfig(
            outer_mfov_overlap=0.04,   # 4%
            inner_sfov_overlap=1.0,    # 1 µm
            pixelsize=10.0,            # 10 nm
            image_filetype="bmp",
            thumbnail_filetype="jpg",
            travel_pattern=TravelPattern.COMB,
            travel_direction=TravelDirection.HORIZONTAL_UP,
            start_position=StartPosition.BOTTOM_LEFT,
            output_folder=r"V:\Fuming\test",
        )
        roi_acq.set_config(config)
        
        # Define ROI vertices (polygon corners in µm)
        vertices = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
        roi_acq.define_roi(vertices)
        
        # Calculate tile positions
        tiles = roi_acq.calculate_tiles()
        
        # Run acquisition
        roi_acq.acquire_all()
    """
    
    # API limits
    HEXAGON_OVERLAP_MIN = 0.0
    HEXAGON_OVERLAP_MAX = 0.5
    SFOV_OVERLAP_MIN = 0.0
    SFOV_OVERLAP_MAX = 6.0
    PIXELSIZE_MIN = 1.0
    PIXELSIZE_MAX = 100.0
    
    def __init__(self, connection: MSEMConnection) -> None:
        self._conn = connection
        self._stage = Stage(connection)
        self._acq = Acquisition(connection)
        self._config: Optional[ROIConfig] = None
        self._tiles: List[TilePosition] = []
        self._progress_callback: Optional[Callable[[ROIProgress], None]] = None
    
    # ========== Configuration ==========
    
    def set_config(self, config: ROIConfig) -> None:
        """Set ROI acquisition configuration"""
        self._validate_config(config)
        self._config = config
        self._apply_config()
    
    def get_config(self) -> Optional[ROIConfig]:
        """Get current configuration"""
        return self._config
    
    def set_defaults(self) -> None:
        """
        Set recommended default configuration.
        
        Defaults:
        - Outer MFOV overlap: 4%
        - Inner SFOV overlap: 1 µm
        - Pixel size: 10 nm
        - Image format: bmp
        - Thumbnail format: jpg
        - Travel pattern: Comb
        - Travel direction: Horizontal Up
        - Start position: Left (Bottom Left)
        """
        config = ROIConfig(
            outer_mfov_overlap=0.04,
            inner_sfov_overlap=1.0,
            pixelsize=10.0,
            image_filetype="bmp",
            thumbnail_filetype="jpg",
            travel_pattern=TravelPattern.COMB,
            travel_direction=TravelDirection.HORIZONTAL_UP,
            start_position=StartPosition.BOTTOM_LEFT,
        )
        self.set_config(config)
    
    def _validate_config(self, config: ROIConfig) -> None:
        """Validate configuration parameters"""
        if not (self.HEXAGON_OVERLAP_MIN <= config.outer_mfov_overlap <= self.HEXAGON_OVERLAP_MAX):
            raise ValueError(
                f"outer_mfov_overlap={config.outer_mfov_overlap} out of range "
                f"[{self.HEXAGON_OVERLAP_MIN}, {self.HEXAGON_OVERLAP_MAX}]"
            )
        if not (self.SFOV_OVERLAP_MIN <= config.inner_sfov_overlap <= self.SFOV_OVERLAP_MAX):
            raise ValueError(
                f"inner_sfov_overlap={config.inner_sfov_overlap} out of range "
                f"[{self.SFOV_OVERLAP_MIN}, {self.SFOV_OVERLAP_MAX}]"
            )
        if not (self.PIXELSIZE_MIN <= config.pixelsize <= self.PIXELSIZE_MAX):
            raise ValueError(
                f"pixelsize={config.pixelsize} out of range "
                f"[{self.PIXELSIZE_MIN}, {self.PIXELSIZE_MAX}]"
            )
    
    def _apply_config(self) -> None:
        """Apply configuration to the microscope"""
        if self._config is None:
            return
        
        # Set overlap parameters
        # AP_HEXAGON_OVERLAP: relative overlap between MFOVs (0-0.5)
        self._conn.set_double("AP_HEXAGON_OVERLAP", self._config.outer_mfov_overlap)
        
        # AP_OVERLAP_X and AP_OVERLAP_Y: overlap in µm for sub-images
        # (AP_OVERLAP sets minimum of both, but we set them explicitly)
        self._conn.set_double("AP_OVERLAP_X", self._config.inner_sfov_overlap)
        self._conn.set_double("AP_OVERLAP_Y", self._config.inner_sfov_overlap)
        
        # Set pixel size
        self._conn.set_double("AP_PIXELSIZE", self._config.pixelsize)
        
        # Set output if specified
        if self._config.output_folder:
            self._acq.set_output_folder(
                folder=self._config.output_folder,
                image_filetype=self._config.image_filetype,
                thumbnail_filetype=self._config.thumbnail_filetype,
            )
        
        # Set experiment metadata
        if self._config.experiment_name:
            self._conn.set_string("STR_EXPERIMENT_NAME", self._config.experiment_name)
        if self._config.sample_name:
            self._conn.set_string("STR_SAMPLE_NAME", self._config.sample_name)
        if self._config.operator_name:
            self._conn.set_string("STR_EXPERIMENT_OPERATOR", self._config.operator_name)
    
    # ========== ROI Definition ==========
    
    def define_roi(self, vertices: List[Tuple[float, float]]) -> int:
        """
        Define Region of Interest by its vertices.
        
        Args:
            vertices: List of (x, y) tuples in µm, defining polygon corners
                     Minimum 3 vertices for a valid polygon
        
        Returns:
            0 on success
        """
        if len(vertices) < 3:
            raise ValueError("ROI must have at least 3 vertices")
        
        # Clear existing ROI
        self._conn.execute("CMD_CLEAR_ROI_VERTICES")
        
        # Add each vertex
        for x, y in vertices:
            self._conn.set_double("AP_ROI_VERTEX_X", x)
            self._conn.set_double("AP_ROI_VERTEX_Y", y)
        
        return 0
    
    def define_rectangular_roi(
        self,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
    ) -> int:
        """
        Define a rectangular ROI.
        
        Args:
            x_min, y_min: Bottom-left corner in µm
            x_max, y_max: Top-right corner in µm
        
        Returns:
            0 on success
        """
        vertices = [
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
        return self.define_roi(vertices)
    
    def clear_roi(self) -> int:
        """Clear the current ROI definition"""
        self._tiles = []
        return self._conn.execute("CMD_CLEAR_ROI_VERTICES")
    
    # ========== Tile Calculation ==========
    
    def calculate_tiles(self) -> List[TilePosition]:
        """
        Calculate tile positions from the defined ROI.
        
        Returns:
            List of TilePosition objects in acquisition order
        """
        # Calculate ROI vertices
        ret = self._conn.execute("CMD_CALCULATE_ROI_VERTICES")
        if ret != 0:
            raise RuntimeError(f"CMD_CALCULATE_ROI_VERTICES failed with code {ret}")
        
        # Get number of tiles
        num_tiles = int(self._conn.get_double("AP_ROI_NUMBEROFPOINTS"))
        
        if num_tiles == 0:
            self._tiles = []
            return self._tiles
        
        # Get tile positions
        tiles = []
        for i in range(num_tiles):
            x = self._conn.get_double_from_list("AP_ROI_XPOSITIONS", i)
            y = self._conn.get_double_from_list("AP_ROI_YPOSITIONS", i)
            tiles.append(TilePosition(x=x, y=y, index=i))
        
        # Organize into grid and sort by travel pattern
        tiles = self._organize_tiles(tiles)
        
        # Apply travel order
        self._tiles = self._apply_travel_order(tiles)
        
        return self._tiles
    
    def get_tiles(self) -> List[TilePosition]:
        """Get previously calculated tiles"""
        return self._tiles
    
    def get_tile_count(self) -> int:
        """Get number of tiles"""
        return len(self._tiles)
    
    def _organize_tiles(self, tiles: List[TilePosition]) -> List[TilePosition]:
        """Organize tiles into a grid structure"""
        if not tiles:
            return tiles
        
        # Find unique Y values (rows) and sort
        y_values = sorted(set(t.y for t in tiles))
        y_to_row = {y: i for i, y in enumerate(y_values)}
        
        # Assign row/col to each tile
        for tile in tiles:
            tile.row = y_to_row[tile.y]
            
        # Within each row, sort by X to get column
        for row_idx in range(len(y_values)):
            row_tiles = [t for t in tiles if t.row == row_idx]
            row_tiles.sort(key=lambda t: t.x)
            for col_idx, tile in enumerate(row_tiles):
                tile.col = col_idx
        
        return tiles
    
    def _apply_travel_order(self, tiles: List[TilePosition]) -> List[TilePosition]:
        """Apply travel pattern and direction to order tiles"""
        if not tiles or self._config is None:
            return tiles
        
        config = self._config
        direction = config.travel_direction
        pattern = config.travel_pattern
        start = config.start_position
        
        # Get grid dimensions
        max_row = max(t.row for t in tiles) if tiles else 0
        max_col = max(t.col for t in tiles) if tiles else 0
        
        # Determine row order based on direction and start
        if direction in (TravelDirection.HORIZONTAL_UP, TravelDirection.VERTICAL_LEFT):
            # Moving up or left: start from bottom
            if start in (StartPosition.TOP_LEFT, StartPosition.TOP_RIGHT):
                row_order = list(range(max_row, -1, -1))  # Top to bottom
            else:
                row_order = list(range(max_row + 1))  # Bottom to top
        else:
            # Moving down or right: start from top
            if start in (StartPosition.BOTTOM_LEFT, StartPosition.BOTTOM_RIGHT):
                row_order = list(range(max_row + 1))  # Bottom to top
            else:
                row_order = list(range(max_row, -1, -1))  # Top to bottom
        
        # Determine column direction based on start
        start_left = start in (StartPosition.BOTTOM_LEFT, StartPosition.TOP_LEFT)
        
        ordered_tiles = []
        for row_num, row in enumerate(row_order):
            row_tiles = [t for t in tiles if t.row == row]
            
            # Sort by column
            if pattern == TravelPattern.COMB:
                # Always same direction
                row_tiles.sort(key=lambda t: t.col, reverse=not start_left)
            else:  # SNAKE
                # Alternate direction each row
                if row_num % 2 == 0:
                    row_tiles.sort(key=lambda t: t.col, reverse=not start_left)
                else:
                    row_tiles.sort(key=lambda t: t.col, reverse=start_left)
            
            ordered_tiles.extend(row_tiles)
        
        return ordered_tiles
    
    # ========== Acquisition ==========
    
    def set_progress_callback(self, callback: Callable[[ROIProgress], None]) -> None:
        """Set callback function for progress updates"""
        self._progress_callback = callback
    
    def acquire_all(
        self,
        tiles: Optional[List[TilePosition]] = None,
        resume_from: int = 0,
    ) -> int:
        """
        Acquire all tiles in the ROI.
        
        This implements a step-and-scan workflow:
        1. Move stage to tile position
        2. Wait for stage to settle
        3. Trigger acquisition
        4. Wait for scan complete (image in buffer)
        5. Move to next position while image writes to disk
        6. Wait for acquisition complete before next scan
        
        Args:
            tiles: Optional list of tiles (uses calculated tiles if None)
            resume_from: Index to resume from (for interrupted acquisitions)
        
        Returns:
            0 on success, negative on error
        """
        if tiles is None:
            tiles = self._tiles
        
        if not tiles:
            raise ValueError("No tiles defined. Call calculate_tiles() first.")
        
        total_tiles = len(tiles)
        
        for i, tile in enumerate(tiles[resume_from:], start=resume_from):
            # Update progress
            if self._progress_callback:
                progress = ROIProgress(
                    current_tile=i + 1,
                    total_tiles=total_tiles,
                    current_x=tile.x,
                    current_y=tile.y,
                    percent_complete=(i + 1) / total_tiles * 100,
                )
                self._progress_callback(progress)
            
            # Move stage
            ret = self._stage.move_xy(tile.x, tile.y, wait=True)
            if ret != 0:
                return ret
            
            # Trigger acquisition
            ret = self._acq.trigger()
            if ret != 0:
                return ret
            
            # Wait for scan complete (safe to move stage now)
            ret = self._acq.wait_scan_complete()
            if ret != 0:
                return ret
            
            # Wait for image to be written before next acquisition
            # (except for last tile where we can let it finish in background)
            if i < total_tiles - 1:
                ret = self._acq.wait_complete()
                if ret != 0:
                    return ret
        
        # Wait for final image
        ret = self._acq.wait_complete()
        
        return ret
    
    def finish_experiment(self) -> int:
        """
        Finish the current experiment.
        Creates metadata files and prepares for next experiment.
        
        Returns:
            0 on success
        """
        return self._conn.execute("CMD_EXPERIMENT_FINISHED")
    
    # ========== Workflow Commands (Janelia Extension) ==========
    
    def get_stored_experiments(self) -> List[dict]:
        """
        Get list of stored experiments (Tile Setups) from ZEN.
        
        Returns:
            List of dicts with keys: id, name, regions, last_change
        """
        result_str = self._conn.get_string("STR_WF_STORED_EXPERIMENTS")
        if not result_str:
            return []
        
        experiments = []
        for exp_str in result_str.split("|"):
            if not exp_str.strip():
                continue
            parts = exp_str.split(";")
            if len(parts) >= 4:
                experiments.append({
                    "id": parts[0],
                    "name": parts[1],
                    "regions": int(parts[2]),
                    "last_change": parts[3],
                })
        
        return experiments
    
    def select_experiment(self, experiment_id: str) -> int:
        """
        Select an experiment (Tile Setup) to run.
        
        Args:
            experiment_id: The experiment ID from get_stored_experiments()
        
        Returns:
            0 on success, -1 if not found
        """
        return self._conn.set_string("STR_WF_SELECTED_EXPERIMENT", experiment_id)
    
    def start_experiment(self) -> int:
        """
        Start the selected experiment.
        
        Returns:
            0: success
            -1: unknown error
            -2: no experiment selected
            1: experiment already running
            2: experiment paused
        """
        return self._conn.execute("CMD_WF_START_EXPERIMENT")
    
    def get_experiment_state(self) -> int:
        """
        Get current experiment state.
        
        Returns:
            0: idle
            -1: error
            1: running
            2: paused
        """
        return self._conn.execute("CMD_WF_STATE")
    
    def pause_experiment(self) -> int:
        """Pause the running experiment"""
        return self._conn.execute("CMD_WF_PAUSE")
    
    def continue_experiment(self) -> int:
        """Continue a paused experiment"""
        return self._conn.execute("CMD_WF_CONTINUE")
    
    def stop_experiment(self) -> int:
        """Stop the running experiment (takes ~10 seconds)"""
        return self._conn.execute("CMD_WF_STOP")
    
    # ========== Utility ==========
    
    def estimate_time(self, tiles: Optional[List[TilePosition]] = None) -> float:
        """
        Estimate total acquisition time in seconds.
        
        This is a rough estimate based on:
        - Scan time per tile (depends on scanrate and image size)
        - Stage movement time (~1-2 seconds per move)
        
        Returns:
            Estimated time in seconds
        """
        if tiles is None:
            tiles = self._tiles
        
        if not tiles:
            return 0.0
        
        # Get scan parameters
        params = self._acq.get_scan_parameters()
        
        # Calculate scan time per tile
        pixels_per_tile = params.width * params.height * 91  # 91 beams
        dwell_time_ns = self._acq.scanrate_to_dwell_time_ns(params.scanrate)
        scan_time_s = pixels_per_tile * dwell_time_ns / 1e9
        
        # Add overhead per tile (stage movement, settling, etc.)
        overhead_per_tile_s = 2.0
        
        time_per_tile = scan_time_s + overhead_per_tile_s
        
        return len(tiles) * time_per_tile

