"""Copyright 2020 The Google Earth Engine Community Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

https://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import datetime
import os
import time
from typing import Any

from absl import app
from absl import flags
import attr
from dateutil import relativedelta
import pytz

import ee
from google3.pyglib.function_utils import memoize

flags.DEFINE_integer('num_utm_grid_cells_l2b', 389, 'UTM grid cell count')
flags.DEFINE_bool(
    'allow_gedi_rasterize_l2b_overwrite', False,
    'Whether exported assets from gedi_rasterize_l2b are allowed to overwrite '
    'existing assets.')

FLAGS = flags.FLAGS


@attr.s
class ExportParameters:
  """Arguments for starting export jobs."""
  asset_id: str = attr.ib()
  image: Any = attr.ib()  # ee.Image
  pyramiding_policy: dict[str, str] = attr.ib()
  crs: str = attr.ib()
  region: Any = attr.ib()  # ee.Geometry.Polygon | ee.Geometry.LinearRing
  overwrite: bool = attr.ib()

# From https://lpdaac.usgs.gov/products/gedi02_av002/
# We list all known property names for safety, even though we might not
# be currently using all of them during rasterization.
INTEGER_PROPS = frozenset({
    'algorithmrun_flag',
    'algorithmrun_flag_aN',
    'channel',
    'degrade_flag',
    'l2a_quality_flag',
    'l2b_quality_flag',
    'landsat_water_persistencoe',
    'leaf_off_flag',
    'leaf_on_cycle',
    'master_int',
    'num_detectedmodes',
    'pft_class',
    'region_class',
    'rg_eg_constraint_center_buffer',
    'rg_eg_flag_aN',
    'rg_eg_niter_aN',
    'selected_l2a_algorithm',
    'selected_mode',
    'selected_mode_flag',
    'selected_rg_algorithm',
    # Note that 'shot_number' is a long ingested as a string, so
    # we don't rasterize it.
    'stale_return_flag',
    'surface_flag',
    'urban_focal_window_size',
    'urban_proportion',
    # Fields added by splitting shot_number
    'minor_frame_number',
    'orbit_number',
    'shot_number_within_beam',
})


def gedi_deltatime_epoch(dt):
  return dt.timestamp() - (datetime.datetime(2018, 1, 1) -
                           datetime.datetime(1970, 1, 1)).total_seconds()


def timestamp_ms_for_datetime(dt):
  return time.mktime(dt.timetuple()) * 1000


def parse_date_from_gedi_filename(table_asset_id):
  return pytz.utc.localize(
      datetime.datetime.strptime(
          os.path.basename(table_asset_id).split('_')[2], '%Y%j%H%M%S'))


def rasterize_gedi_by_utm_zone(table_asset_ids,
                               raster_asset_id,
                               grid_cell_feature,
                               grill_month,
                               overwrite=False):
  """Creates and runs an EE export job.

  Args:
    table_asset_ids: list of strings, table asset ids to rasterize
    raster_asset_id: string, raster asset id to create
    grill_month: grilled Month
    grid_cell_feature: ee.Feature
    overwrite: bool, if any of the assets can be replaced if they already exist

  Returns:
    string, task id of the created task
  """
  export_params = create_export(table_asset_ids, raster_asset_id,
                                grid_cell_feature, grill_month, overwrite)
  return _start_task(export_params)


def create_export(table_asset_ids: list[str], raster_asset_id: str,
                  grid_cell_feature: Any, grill_month: datetime.datetime,
                  overwrite: bool) -> ExportParameters:
  """Creates an EE export job definition.

  Args:
    table_asset_ids: list of strings, table asset ids to rasterize
    raster_asset_id: string, raster asset id to create
    grid_cell_feature: ee.Feature
    grill_month: grilled month

  Returns:
    an ExportParameters object containing arguments for an export job.
  """
  if not table_asset_ids:
    raise ValueError('No table asset ids specified')
  table_asset_dts = []
  for asset_id in table_asset_ids:
    date_obj = parse_date_from_gedi_filename(asset_id)
    table_asset_dts.append(date_obj)
  # pylint:disable=g-tzinfo-datetime
  # We don't care about pytz problems with DST - this is just UTC.
  month_start = grill_month.replace(day=1)
  # pylint:enable=g-tzinfo-datetime
  month_end = month_start + relativedelta.relativedelta(months=1)
  if all((date < month_start or date >= month_end) for date in table_asset_dts):
    raise ValueError(
        'ALL the table files are outside of the expected month that is ranging'
        ' from %s to %s' % (month_start, month_end))

  right_month_dts = [
      dates for dates in table_asset_dts
      if dates >= month_start and dates < month_end
  ]
  if len(right_month_dts) / len(table_asset_dts) < 0.95:
    raise ValueError(
        'The majority of table ids are not in the requested month %s' %
        grill_month)

  @memoize.Memoize()
  def get_raster_bands(band):
    return [band + str(count) for count in range(30)]

  # This is a subset of all available table properties.
  raster_bands = [
      'algorithmrun_flag', 'beam', 'cover'
  ] + get_raster_bands('cover_z') + [
      'degrade_flag', 'delta_time', 'fhd_normal', 'l2b_quality_flag',
      'local_beam_azimuth', 'local_beam_elevation', 'pai'
  ] + get_raster_bands('pai_z') + get_raster_bands('pavd_z') + [
      'pgap_theta', 'selected_l2a_algorithm', 'selected_rg_algorithm',
      'sensitivity', 'solar_azimuth', 'solar_elevation',
      'minor_frame_number', 'orbit_number', 'shot_number_within_beam'
  ]

  shots = []
  for table_asset_id in table_asset_ids:
    shots.append(ee.FeatureCollection(table_asset_id))

  box = grid_cell_feature.geometry().buffer(2500, 25).bounds()
  # month_start and month_end are converted to epochs using the
  # same scale as "delta_time."
  # pytype: disable=attribute-error
  shots = ee.FeatureCollection(shots).flatten().filterBounds(box).filter(
      ee.Filter.rangeContains(
          'delta_time',
          gedi_deltatime_epoch(month_start),
          gedi_deltatime_epoch(month_end))
    )
  # pytype: enable=attribute-error
  # We use ee.Reducer.first() below, so this will pick the point with the
  # higherst sensitivity.
  shots = shots.sort('sensitivity', False)

  crs = grid_cell_feature.get('crs').getInfo()

  image_properties = {
      'month': grill_month.month,
      'year': grill_month.year,
      'version': 1,
      'system:time_start': timestamp_ms_for_datetime(month_start),
      'system:time_end': timestamp_ms_for_datetime(month_end),
      'table_asset_ids': table_asset_ids
  }

  image = (
      shots.sort('sensitivity', False).reduceToImage(
          raster_bands,
          ee.Reducer.first().forEach(raster_bands)).reproject(
              crs, None, 25).set(image_properties))

  int_bands = [p for p in raster_bands if p in INTEGER_PROPS]
  # This keeps the original (alphabetic) band order.
  image_with_types = image.toDouble().addBands(
      image.select(int_bands).toInt(), overwrite=True)

  return ExportParameters(
      asset_id=raster_asset_id,
      image=image_with_types.clip(box),
      pyramiding_policy={'.default': 'sample'},
      crs=crs,
      region=box,
      overwrite=overwrite)


def _start_task(export_params: ExportParameters) -> str:
  """Starts an EE export task with the given parameters."""
  asset_id = export_params.asset_id
  task = ee.batch.Export.image.toAsset(
      image=export_params.image,
      description=os.path.basename(asset_id),
      assetId=asset_id,
      region=export_params.region,
      pyramidingPolicy=export_params.pyramiding_policy,
      scale=25,
      crs=export_params.crs,
      maxPixels=1e13,
      overwrite=export_params.overwrite)

  time.sleep(0.1)
  task.start()

  return task.status()['id']


def main(argv):
  start_id = 1  # First UTM grid cell id
  ee.Initialize()
  raster_collection = 'LARSE/GEDI/GEDI02_B_002_MONTHLY'

  for grid_cell_id in range(start_id, start_id + FLAGS.num_utm_grid_cells_l2b):
    grid_cell_feature = ee.Feature(
        ee.FeatureCollection(
            'users/yang/GEETables/GEDI/GEDI_UTM_GRIDS_LandOnly').filterMetadata(
                'grid_id', 'equals', grid_cell_id)).first()
    with open(argv[1]) as fh:
      rasterize_gedi_by_utm_zone(
          [x.strip() for x in fh],
          raster_collection + '/' + '%03d' % grid_cell_id,
          grid_cell_feature,
          argv[2],
          overwrite=FLAGS.allow_gedi_rasterize_l2b_overwrite)


if __name__ == '__main__':
  app.run(main)
