

import logging
import tempfile

# import cftime
from datetime import datetime, timedelta

from pathlib import Path

import math

import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform_geom, transform_bounds

from pygeoapi.provider.base import (BaseProvider,
                                    ProviderConnectionError,
                                    ProviderNoDataError,
                                    ProviderQueryError)
from msc_pygeoapi.provider.climate_xarray import (ClimateProvider,
                                                  open_data)

from msc_pygeoapi.util import remove_z_from_bbox

from msc_pygeoapi.env import GEOMET_HPFX_BASEPATH

LOGGER = logging.getLogger(__name__)

RIOPS_ARCHIVES_FORECAST_BASEPATH = (
    f'{GEOMET_HPFX_BASEPATH}/model_riops/netcdf/forecast/polar_stereographic'
)


class RIOPSProvider(ClimateProvider):
    """RIOPS Provider"""

    def __init__(self, provider_def):
        """
        Initialize object
        :param provider_def: provider definition
        :returns: pygeoapi.provider.xarray_.XarrayProvider
        """
        BaseProvider.__init__(self, provider_def)

        try:
            self.period = 'P1H'

            self.variables = self.options['fields']

            self.three_dim_vars = [
                'vomecrty',
                'vosaline',
                'votemper',
                'vozocrtx'
            ]

            self.file_list = self._get_files_list()

            latest_three_dim_runs = \
                self.file_list[self.model_run_keys[-1]]['3d']

            forecast_hours = \
                list(latest_three_dim_runs.keys())
            latest_hour = forecast_hours[-1]

            single_file = \
                str(
                    latest_three_dim_runs[latest_hour][-1]
                )

            self._data = open_data(single_file)
            self._coverage_properties = self.get_coverage_properties()

            self.axes = [self._coverage_properties['x_axis_label'],
                         self._coverage_properties['y_axis_label']]

            self.axes.append('reference_time')
            self.axes.append('depth')

            self.get_fields()

        except Exception as err:
            LOGGER.warning(err)
            raise ProviderConnectionError(err)

    def _get_files_list(self):
        self.current_date = self.obtain_reference_time()

        files = sorted(
            file_
            for file_ in Path(
                f'{RIOPS_ARCHIVES_FORECAST_BASEPATH}'
            ).rglob(f'{self.current_date}*.nc')
        )

        files_dict = {}
        for file_ in files:

            model_run = file_.name.split('_')[0]
            if model_run not in files_dict:
                files_dict[model_run] = {}

            spatial_dim = str(file_).split('/')[9]
            if spatial_dim not in files_dict[model_run]:
                files_dict[model_run][spatial_dim] = {}

            forecast_hour = file_.name.split('_')[-1].split('.')[0]
            if forecast_hour not in files_dict[model_run][spatial_dim]:
                files_dict[model_run][spatial_dim][forecast_hour] = []

            files_dict[model_run][spatial_dim][forecast_hour].append(file_)

        self.model_run_keys = list(files_dict.keys())

        latest_model_run = self.model_run_keys[-1]

        # Need to check both 2d and 3d variables
        spatial_dims = ['2d', '3d']
        for spatial_dim in spatial_dims:
            if spatial_dim == '2d':
                num_forecast_files = len(self.variables)
            else:
                num_forecast_files = len(self.three_dim_vars)

            forecast_hours = \
                list(files_dict[latest_model_run][spatial_dim].keys())

            if len(forecast_hours) < 85:
                # Need to fallback to the last model run
                self.model_run_fallback(files_dict)
                return files_dict

            for hour in forecast_hours:
                # Ensure that the hour contains the needed amount of files
                if (
                    len(files_dict[latest_model_run][spatial_dim][hour]) <
                    num_forecast_files
                ):
                    # Need to fallback to the last model run
                    self.model_run_fallback(files_dict)
                    return files_dict

        return files_dict

    def model_run_fallback(self, files_dict):
        '''
        Latest available model run is not complete
        '''

        latest_run = self.model_run_keys[-1]
        files_dict.pop(latest_run)

        if len(list(files_dict.keys())) == 0:
            # This means we need to use the previous date
            today = datetime.strptime(self.current_date, '%Y%m%d')
            fallback = today - timedelta(days=1)
            date = fallback.strftime('%Y%m%d')
            self.current_date = date

            # Next need to get the files
            files = sorted(
                file_
                for file_ in Path(
                    f'{RIOPS_ARCHIVES_FORECAST_BASEPATH}'
                ).rglob(f'{self.current_date}*.nc')
            )
            for file_ in files:
                model_run = file_.name.split('_')[0]
                if model_run not in files_dict:
                    files_dict[model_run] = {}

                spatial_dim = str(file_).split('/')[9]
                if spatial_dim not in files_dict[model_run]:
                    files_dict[model_run][spatial_dim] = {}

                forecast_hour = file_.name.split('_')[-1]
                if forecast_hour not in files_dict[model_run][spatial_dim]:
                    files_dict[model_run][spatial_dim][forecast_hour] = []

                files_dict[model_run][spatial_dim][forecast_hour].append(file_)

        self.model_run_keys = list(files_dict.keys())

    def obtain_reference_time(self):
        '''
        Use the files to find the correct date
        '''
        dir_path = Path(f'{RIOPS_ARCHIVES_FORECAST_BASEPATH}/2d')

        # sorted needed to ensure last file in list was produced today
        one_file = sorted(
            file_.name
            for file_ in dir_path.rglob('*.nc')
        )[-1]

        # Now need to extract the date
        date = one_file.split('_')[0].split('T')[0]
        return date

    def get_coverage_domainset(self):
        domainset = super().get_coverage_domainset()

        if ('percentile' in domainset):
            domainset.pop('percentile')

        # Can allow the acceptance of a reference_time?
        # Use the current date

        first_model_run = datetime.strptime(
            self.model_run_keys[0], '%Y%m%dT%HZ'
        ).strftime('%Y-%m-%dT%HZ')

        last_model_run = datetime.strptime(
            self.model_run_keys[-1], '%Y%m%dT%HZ'
        ).strftime('%Y-%m-%dT%HZ')

        domainset['reference_time'] = {
            'definition': 'reference_time - Temporal',
            'interval': [first_model_run, last_model_run]
        }

        lower_depth = self._data.coords['depth'].values[0]
        upper_depth = self._data.coords['depth'].values[-1]

        domainset['depth'] = {
            'definition': 'depth',
            'interval': [lower_depth, upper_depth]
        }

        return domainset

    def get_coverage_properties(self):
        cov_properties = self._get_coverage_properties()

        try:
            # self._data is one of the files from the latest model run
            # so it will contain the upper datetime bound
            end_date = self._data.coords[self.time_field].values[0]
            end_date_str = end_date.astype('datetime64[h]').astype(str)
            end_formatted = datetime.strptime(
                end_date_str, '%Y-%m-%dT%H'
            ).strftime('%Y-%m-%dT%HZ')

            # the lower bound will be from the first reference time
            start_formatted = datetime.strptime(
                self.model_run_keys[0], '%Y%m%dT%HZ'
            ).strftime('%Y-%m-%dT%HZ')

            cov_properties['time_range'] = [
                start_formatted,
                end_formatted
            ]

            cov_properties['restime'] = self.period

        except Exception as err:
            LOGGER.error(err)

        return cov_properties

    def get_fields(self):
        """
        Get fields

        :returns: `dict` of fields
        """

        LOGGER.debug('Getting fields')

        latest_two_dim_run = self.file_list[self.model_run_keys[-1]]['2d']
        latest_forecast = list(latest_two_dim_run.keys())[-1]

        iter_files = (
            file_
            for file_ in latest_two_dim_run[latest_forecast]
        )

        for file in iter_files:
            file_contents = open_data(str(file))

            # Take the unique variable, get its metadata
            for name, var in file_contents.variables.items():
                LOGGER.debug(f'Determining rangetype for {name}')
                desc, units = None, None
                if name in self.variables and len(var.shape) >= 2:
                    parameter = self._get_parameter_metadata(
                        name, var.attrs)
                    desc = parameter['description']
                    units = parameter['unit_label']

                    self.dtype = var.dtype
                    if self.dtype.name.startswith('float'):
                        self.dtype = 'float'
                    elif self.dtype.name.startswith('int'):
                        self.dtype = 'integer'
                    elif self.dtype.startswith('str'):
                        self.dtype = 'string'

                    uom_id = f'http://www.opengis.net/def/uom/UCUM/{units}'
                    encoding_data_type = (
                        'http://www.opengis.net/def/dataType/OGC/0/'
                        f'{var.dtype}'
                    )

                    self._fields[name] = {
                        'title': var.attrs.get('long_name') or desc,
                        'type': self.dtype,
                        'x-ogc-unit': units,
                        '_meta': {
                            'tags': var.attrs,
                            'uom': {
                                'id': uom_id, # noqa
                                'type': 'UnitReference',
                                'code': units
                            },
                            'encodingInfo': {
                                'dataType': encoding_data_type # noqa
                            },
                            'nodata': 'null',
                        }
                    }
        return self._fields

    def query(self, properties=[], subsets={},
              bbox=[], datetime_=None, format_='json'):

        file_crs = '+proj=stere +lat_0=90 +lon_0=-100 +k=0.93301243 +x_0=4245000 +y_0=5295000 +R=6371229 +units=m +no_defs' # noqa
        crs_dest = CRS.from_string(file_crs)
        # LOGGER.debug(crs_dest)

        depth_levels = []

        # reference_time with the hour
        if 'reference_time' in subsets:
            self.reference_time = subsets['reference_time'][0]

            # Can use the model run keys to determine allowed reference times
            try:
                selected_date = datetime.strptime(
                    self.reference_time, '%Y-%m-%dT%HZ'
                )
                file_ref_time = selected_date.strftime('%Y%m%dT%HZ')

                if file_ref_time not in self.model_run_keys:
                    allowed_times = ''

                    for x in self.model_run_keys:
                        time_formatted = datetime.strptime(
                            x, '%Y%m%dT%HZ'
                        ).strftime('%Y-%m-%dT%HZ')
                        if x == self.model_run_keys[-1]:
                            allowed_times = \
                                allowed_times + 'or ' + f'{time_formatted}'
                        else:
                            allowed_times = \
                                allowed_times + f'{time_formatted}, '
                    err = (
                        'Invalid reference_time value provided. '
                        f'Value must be {allowed_times}.'
                    )
                    LOGGER.error(err)
                    raise ProviderQueryError(err, user_msg=err)

                # Get the hour
                ref_hour = self.reference_time.split(
                    'T')[1].split('Z')[0]
                self.data = self.data.replace(
                    '/*/', f'/{ref_hour}/', 1
                )
                self.data = self.data.replace('/*_', f'/{file_ref_time}_', 1)

                self.ref_datetime = selected_date

            except ValueError as err:
                user_msg = (
                    'Invalid reference_time value provided. '
                    'Value must be YYYY-MM-DDTHHZ.'
                )
                LOGGER.error(user_msg)
                raise ProviderQueryError(err, user_msg=user_msg)
        else:
            # Use latest model run by default
            self.reference_time = self.model_run_keys[-1]
            selected_date = datetime.strptime(
                self.reference_time, '%Y%m%dT%HZ'
            )

            ref_hour = self.reference_time.split(
                'T')[1].split('Z')[0]
            self.data = self.data.replace(
                '/*/', f'/{ref_hour}/', 1
            )
            self.data = self.data.replace('/*_', f'/{self.reference_time}_', 1)
            self.ref_datetime = selected_date

        if not datetime_:
            # Setting to closest available forecast hour

            now = datetime.now()

            diff = int((now - self.ref_datetime).total_seconds()/3600)

            if diff >= 84:
                # Use the latest forecast hour
                self.data = self.data.replace(
                    '/*/', '/084/'
                )
                self.data = self.data.replace(
                    'P*', 'P084'
                )
            else:
                if diff < 10:
                    forecast_hour = f'00{diff}'
                else:
                    forecast_hour = f'0{diff}'
                self.data = self.data.replace(
                    '/*/', f'/{forecast_hour}/'
                )
                self.data = self.data.replace(
                    'P*', f'P{forecast_hour}'
                )
        elif datetime_ and '/' not in datetime_:
            # Use this to insert a forecast hour into the path
            # reference_time has been decided on, check if within bounds
            try:
                date = datetime.strptime(
                    datetime_, '%Y-%m-%dT%HZ'
                )

                diff = int((date - self.ref_datetime).total_seconds()/3600)
                if (
                    diff < 0 or
                    diff > 84
                ):
                    # Invalid datetime for the reference time
                    if 'reference_time' in subsets:
                        err = (
                            'Invalid datetime value provided for the '
                            'reference_time. Please choose a datetime '
                            'value occuring 0 to 84 hours after the '
                            'reference_time.'
                        )
                    else:
                        err = (
                            'Invalid datetime value provided. Currently using '
                            'the latest available model run.'
                        )
                    LOGGER.error(err)
                    raise ProviderQueryError(err, user_msg=err)

                # Apply the forecast hour to the data path
                if diff < 10:
                    forecast_hour = f'00{diff}'
                else:
                    forecast_hour = f'0{diff}'
                self.data = self.data.replace(
                    '/*/', f'/{forecast_hour}/'
                )
                self.data = self.data.replace(
                    'P*', f'P{forecast_hour}'
                )

            except ValueError as err:
                user_msg = (
                    'Invalid datetime value provided. '
                    'Value must be YYYY-MM-DDTHHZ or '
                    'YYYY-MM-DDTHHZ/YYYY-MM-DDTHHZ for a date range.'
                )
                LOGGER.error(err)
                raise ProviderQueryError(err, user_msg=user_msg)

        if 'depth' in subsets:
            # Working with 3d data
            if len(properties) > 0:
                for var in properties:
                    if var not in self.three_dim_vars:
                        err = 'Invalid variable specified for 3d data.'
                        LOGGER.error(err)
                        raise ProviderQueryError(err, user_msg=err)
            self.variables = self.three_dim_vars
            self.data = self.data.replace('/2d/', '/3d/')

            # Ensure that the depth levels are float values
            try:
                for depth in subsets['depth']:
                    depth_levels.append(float(depth))
            except ValueError as err:
                user_msg = (
                    'Invalid depth value provided. '
                    'Value should be float or like '
                    '0.5:8 which specifies depth levels '
                    'between 0.5m and 8m.'
                )
                LOGGER.error(err)
                raise ProviderQueryError(err, user_msg=user_msg)

        # Initialize file name
        self.filename = self.data.split('/')[-1]

        # set default variable if properties is None
        properties_ = properties.copy()
        LOGGER.debug(properties_)

        # TODO Confirm if the below is needed?
        if len(properties) > 1 and format_ == 'json':
            err = 'Only a single property value is supported for CovJSON.'
            LOGGER.error(err)
            raise ProviderQueryError(err, user_msg=err)
        if not properties:
            properties_.append(self.variables[0])
        if len(properties_) > 1:
            # Check if different vertical levels
            self.filename = self.filename.replace(
                '_*_', 'MulVar', 1
            )

            # If depth levels set, it is a 3d request
            # otherwise check the variables used to find the
            # vertical levels
            if not depth_levels:
                sfc_queried_props = list(
                    set(properties_) - set(self.three_dim_vars)
                )
                if (
                    len(sfc_queried_props) > 0
                ):
                    depth_levels.append('SFC')

                dbs_queried_props = list(
                    set(properties_) & set(self.three_dim_vars)
                )
                if (
                    len(dbs_queried_props) > 0
                ):
                    depth_levels.append('DBS-0.5m')

                if len(depth_levels) > 1:
                    self.filename = self.filename.replace(
                        '_*_', '_MulDepth_'
                    )
                else:
                    self.filename = self.filename.replace(
                        '_*_', '_' + depth_levels[0] + '_'
                    )

                    self.data = self.data.replace(
                        '_*_', '_' + depth_levels[0] + '_'
                    )

        else:
            self.filename = self.filename.replace(
                '_*_', '_' + properties_[0].upper() + '_', 1
            )
            self.data = self.data.replace(
                '_*_', '_' + properties_[0].upper() + '_', 1
            )
            # LOGGER.debug(self.data)
            if not depth_levels:
                if properties_[0] in self.three_dim_vars:
                    vertical_level = 'DBS-0.5m'
                else:
                    vertical_level = 'SFC'
                self.filename = self.filename.replace(
                    '_*_', '_' + vertical_level + '_'
                )

        # LOGGER.debug(self.data)
        self._data = open_data(self.data)
        # LOGGER.debug(type(self._data))
        # LOGGER.debug(self._data)
        # LOGGER.debug(self._data.variables)
        # LOGGER.debug(self._data.variables.items())
        # LOGGER.debug(self._data['polar_stereographic'].attrs)
        # LOGGER.debug(self._data['iiceconc'].attrs)

        properties_ = properties_ + ['polar_stereographic']
        data = self._data[[*properties_]]

        if any([bbox, datetime_ is not None, 'depth' in subsets]):

            LOGGER.debug('Creating spatio-temporal subset')

            query_params = {}

            if bbox and len(bbox) > 0:
                # TODO Need to check that the CRS conversion works
                bbox = remove_z_from_bbox(bbox)
                minx, miny, maxx, maxy = bbox

                crs_src = CRS.from_epsg(4326)

                LOGGER.debug('source bbox CRS and data CRS are different')
                LOGGER.debug('reprojecting bbox into native coordinates')

                temp_geom_min = {"type": "Point", "coordinates": [minx, miny]}
                temp_geom_max = {"type": "Point", "coordinates": [maxx, maxy]}
                LOGGER.debug(temp_geom_min)
                LOGGER.debug(temp_geom_max)

                min_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                         temp_geom_min)
                minx2, miny2 = min_coord['coordinates']

                max_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                         temp_geom_max)
                maxx2, maxy2 = max_coord['coordinates']

                LOGGER.debug(f'Source coordinates: {minx}, {miny}, {maxx}, {maxy}') # noqa
                LOGGER.debug(f'Destination coordinates: {minx2}, {miny2}, {maxx2}, {maxy2}')  # noqa

                # file_ = ''
                # crs_des = CRS.from_epsg(4326)
                # with rasterio.open(file_) as src:
                #     other_bounds = transform_bounds(crs_des, src.crs, -180, 28.896259522534894, 180.0, 90.0)
                #     LOGGER.debug(f'{other_bounds=}')

                #     wgs84_bounds_proj4 = transform_bounds(crs_dest, crs_des, *src.bounds)
                #     LOGGER.debug(f'{wgs84_bounds_proj4=}')

                #     LOGGER.debug(f"File bounds: {src.bounds}")
                #     wgs84_bounds = transform_bounds(src.crs, crs_des, *src.bounds)
                #     LOGGER.debug(f'{wgs84_bounds=}')

                bbox = [minx2, miny2, maxx2, maxy2]
                # bbox = [-2500, -2500, 8847500, 8047500]

                query_params[self._coverage_properties['x_axis_label']] = \
                    slice(bbox[0], bbox[2])

                self._coverage_properties['time_axis_label']

                lat = self._data.coords[self.y_field]
                lat_field = self._coverage_properties['y_axis_label']

                if lat.values[1] > lat.values[0]:
                    query_params[lat_field] = \
                        slice(bbox[1], bbox[3])
                else:
                    query_params[lat_field] = \
                        slice(bbox[3], bbox[1])

            if datetime_ is not None and '/' in datetime_:
                try:
                    begin, end = datetime_.split('/')

                    begin_datetime = datetime.strptime(
                        begin, '%Y-%m-%dT%HZ'
                    )
                    end_datetime = datetime.strptime(
                        end, '%Y-%m-%dT%HZ'
                    )

                    # Ensure that the datetime is valid for the
                    # reference time
                    first_diff = (
                        begin_datetime - self.ref_datetime
                    ).total_seconds()/3600
                    second_diff = (
                        end_datetime - self.ref_datetime
                    ).total_seconds()/3600

                    if (
                        first_diff < 0 or
                        second_diff < 0 or
                        first_diff > 84 or
                        second_diff > 84
                    ):
                        if 'reference_time' in subsets:
                            err = (
                                'Invalid datetime value provided for the '
                                'reference_time. Please choose a datetime '
                                'value occuring 0 to 84 hours after the '
                                'reference_time.'
                            )
                        else:
                            err = (
                                'Invalid datetime value provided for the '
                                'latest model run.'
                            )
                        LOGGER.error(err)
                        raise ProviderQueryError(err, user_msg=err)

                    if begin_datetime < end_datetime:
                        slice_start = begin[:-1]
                        forecast_start = first_diff
                        slice_end = end[:-1]
                        forecast_end = second_diff
                    else:
                        LOGGER.debug('Reversing slicing from high to low')
                        slice_start = end[:-1]
                        forecast_start = second_diff
                        slice_end = begin[:-1]
                        forecast_end = first_diff
                    query_params[self.time_field] = slice(
                        slice_start, slice_end)

                    # Add to the filename
                    forecast_range = ''
                    if forecast_start < 10:
                        forecast_range = f'P00{forecast_start}-'
                    else:
                        forecast_range = f'P0{forecast_start}-'
                    if forecast_end < 10:
                        forecast_range = \
                            forecast_range + f'P00{forecast_end}'
                    else:
                        forecast_range = \
                            forecast_range + f'P0{forecast_end}'
                    self.filename = self.filename.replace(
                        'P*', forecast_range
                    )
                except ValueError as err:
                    user_msg = (
                        'Invalid datetime value provided. '
                        'Value must be YYYY-MM-DDTHHZ or '
                        'YYYY-MM-DDTHHZ/YYYY-MM-DDTHHZ for a date range.'
                    )
                    LOGGER.error(err)
                    raise ProviderQueryError(err, user_msg=user_msg)

            if 'depth' in subsets:
                try:
                    if len(subsets['depth']) > 1:
                        lower_depth = float(subsets['depth'][0])
                        upper_depth = float(subsets['depth'][-1])

                        self.filename = self.filename.replace(
                            '_*_', '_MulDepth_'
                        )
                    else:
                        lower_depth = float(subsets['depth'][0])
                        upper_depth = float(math.ceil(subsets['depth'][0]))

                        self.filename = self.filename.replace(
                            '_*_', '_' + str(subsets['depth'][0]) + '_'
                        )
                    query_params['depth'] = slice(lower_depth, upper_depth)
                except (ValueError, IndexError) as err:
                    user_msg = (
                        'Invalid depth value provided. '
                        'Value should be float or like '
                        '0.5:8 which specifies depth levels '
                        'between 0.5m and 8m.'
                    )
                    LOGGER.error(err)
                    raise ProviderQueryError(err, user_msg=user_msg)

            LOGGER.debug(f'Query parameters: {query_params}')
            try:
                data = data.loc[query_params]
            except Exception as err:
                LOGGER.warning(err)
                raise ProviderQueryError(err)

        if (any([data.coords[self.x_field].size == 0,
                data.coords[self.y_field].size == 0])):
            msg = 'No data found'
            LOGGER.warning(msg)
            raise ProviderNoDataError(msg)

        out_meta = {
            'bbox': [
                data.coords[self.x_field].values[0],
                data.coords[self.y_field].values[0],
                data.coords[self.x_field].values[-1],
                data.coords[self.y_field].values[-1]
            ],
            'time': [None, None],
            "driver": "xarray",
            "height": data.sizes[self.y_field],
            "width": data.sizes[self.x_field],
            "time_steps": 1,
            "variables": {var_name: var.attrs
                          for var_name, var in data.variables.items()}
        }

        meta_start = data.coords[self.time_field].values[0]
        meta_start = meta_start.astype('datetime64[h]').astype(str)

        meta_end = data.coords[self.time_field].values[-1]
        meta_end = meta_end.astype('datetime64[h]').astype(str)

        out_meta['time'] = [
            meta_start,
            meta_end
        ]
        out_meta['time_steps'] = data.sizes[self.time_field]

        LOGGER.debug('Serializing data in memory')
        if format_ == 'json':
            LOGGER.debug('Creating output in CoverageJSON')
            return self.gen_covjson(out_meta, data, properties_)
        else:
            with tempfile.TemporaryFile() as fp:
                LOGGER.debug('Returning data in native NetCDF format')
                fp.write(data.to_netcdf())
                fp.seek(0)
                return fp.read()

    def gen_covjson(self, metadata, data, range_type):

        cj = super().gen_covjson(metadata, data, range_type)
        cj['parameters'].pop('polar_stereographic')
        cj['ranges'].pop('polar_stereographic')

        return cj
