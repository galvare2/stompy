"""
Read a suntans grid in the current directory and write a DFM grid, output_net.nc
"""

from stompy.grid import unstructured_grid
from stompy.model.delft import dfm_grid

ug=unstructured_grid.SuntansGrid(".")

dfm_grid.write_dfm(ug,"TX_output_net.nc")

"""
- Convert xyz to tiff using GDAL
- Use the tiff for elevation for the mesh

"""

