"""Define high level structure StagyyData"""

import bisect
import re
import os.path
import numpy as np
from . import constants, stagyyparsers


UNDETERMINED = object()
# dummy object with a unique identifier,
# useful to mark stuff as yet undetermined,
# as opposed to either some value or None if
# non existent


class _Geometry:

    """Geometry information"""

    _regexes = (re.compile(r'^n([xyztprb])tot$'),  # ntot
                re.compile(r'^([xyztpr])_coord$'),  # coord
                re.compile(r'^([xyz])_mesh$'),  # cartesian mesh
                re.compile(r'^([tpr])_mesh$'))  # curvilinear mesh

    def __init__(self, header, par):
        self._header = header
        self._par = par
        self._coords = None
        self._cart_meshes = None
        self._curv_meshes = None
        self._shape = {'sph': False, 'cyl': False, 'axi': False,
                       'ntot': list(header['nts']) + [header['ntb']]}
        shape = self._par['geometry']['shape'].lower()
        aspect = self._header['aspect']
        if self.rcmb is not None and self.rcmb >= 0:
            # curvilinear
            self._shape['cyl'] = self.twod_xz and (shape == 'cylindrical' or
                                                   aspect[0] >= np.pi)
            self._shape['sph'] = not self._shape['cyl']
        elif self.rcmb is None:
            header['rcmb'] = self._par['geometry']['r_cmb']
            if self.rcmb >= 0:
                if self.twod_xz and shape == 'cylindrical':
                    self._shape['cyl'] = True
                elif shape == 'spherical':
                    self._shape['sph'] = True
        self._shape['axi'] = self.cartesian and self.twod_xz and \
            shape == 'axisymmetric'

        self._coords = (header['e1_coord'],
                        header['e2_coord'],
                        header['e3_coord'])

        if self.cartesian:
            self._cart_meshes = np.meshgrid(self.x_coord, self.y_coord,
                                            self.z_coord, indexing='ij')
            self._curv_meshes = (None, None, None)
        else:
            t_mesh, p_mesh, r_mesh = np.meshgrid(
                self.x_coord, self.y_coord, self.z_coord + self.rcmb,
                indexing='ij')
            # compute cartesian coordinates
            # z along rotation axis at theta=0
            # x at th=90, phi=0
            # y at th=90, phi=90
            x_mesh = r_mesh * np.cos(p_mesh) * np.sin(t_mesh)
            y_mesh = r_mesh * np.sin(p_mesh) * np.sin(t_mesh)
            z_mesh = r_mesh * np.cos(t_mesh)
            self._cart_meshes = (x_mesh, y_mesh, z_mesh)
            self._curv_meshes = (t_mesh, p_mesh, r_mesh)

        # spherical annulus
        # need to add a phi row to have a continuous field
        # should be done on fields and meshes as well
        # self.th_coord = np.array(np.pi / 2)
        # self._ph_coord = e2_coord
        # self.ph_coord = np.append(e2_coord, e2_coord[1]-e2_coord[0])

    @property
    def cartesian(self):
        """Cartesian geometry"""
        return not self.curvilinear

    @property
    def curvilinear(self):
        """Spherical or cylindrical geometry"""
        return self.spherical or self.cylindrical

    @property
    def cylindrical(self):
        """Cylindrical geometry (2D spherical)"""
        return self._shape['cyl']

    @property
    def spherical(self):
        """Spherical geometry"""
        return self._shape['sph']

    @property
    def yinyang(self):
        """Yin-yang geometry (3D spherical)"""
        return self.spherical and self.nbtot == 2

    @property
    def twod_xz(self):
        """XZ plane only"""
        return self.nytot == 1

    @property
    def twod_yz(self):
        """YZ plane only"""
        return self.nxtot == 1

    @property
    def twod(self):
        """2D geometry"""
        return self.twod_xz or self.twod_yz

    @property
    def threed(self):
        """3D geometry"""
        return not self.twod

    def __getattr__(self, attr):
        # provide nDtot, D_coord, D_mesh and nbtot
        # with D = x, y, z or t, p, r
        for reg, dat in zip(self._regexes, (self._shape['ntot'],
                                            self._coords,
                                            self._cart_meshes,
                                            self._curv_meshes)):
            match = reg.match(attr)
            if match is not None:
                return dat['xtypzrb'.index(match.group(1)) // 2]
        return self._header[attr]


class _Rprof(np.ndarray):  # _TimeSeries also

    """Wrap rprof data"""

    def __new__(cls, data, times, isteps):
        cls._check_args(data, times, isteps)
        obj = np.asarray(data).view(cls)
        return obj

    def __init__(self, data, times, isteps):
        _Rprof._check_args(data, times, isteps)
        self._times = times
        self._isteps = isteps

    def __array_finalize__(self, obj):
        if obj is None:
            return
        self._times = getattr(obj, 'times', [])
        self._isteps = getattr(obj, 'isteps', [])

    def __getitem__(self, key):
        try:
            key = constants.RPROF_VAR_LIST[key].prof_idx
        except KeyError:
            pass
        return super().__getitem__(key)

    @staticmethod
    def _check_args(data, times, isteps):
        if not len(data) == len(times) == len(isteps):
            raise ValueError('Inconsistent lengths in rprof data')

    @property
    def times(self):
        """Advective time of each rprof"""
        return self._times

    @property
    def isteps(self):
        """istep of each rprof"""
        return self._isteps


class _Fields(dict):

    """Wrap fields of a step"""

    def __init__(self, step):
        self.step = step
        self._header = UNDETERMINED
        self._geom = UNDETERMINED
        super().__init__()

    def __missing__(self, name):
        if name not in constants.FIELD_VAR_LIST:
            raise ValueError("Unknown field variable: '{}'".format(name))
        par_type = constants.FIELD_VAR_LIST[name].par
        fieldfile = self.step.sdat.filename(par_type, self.step.isnap)
        header, fields = stagyyparsers.fields(fieldfile)
        self._header = header
        if par_type == 'vp':
            fld_names = ['u', 'v', 'w', 'p']
        elif par_type == 'sx':
            fld_names = ['sx', 'sy', 'sz', 'x']
        else:
            fld_names = [name]  # wrong for some stuff like stream func
        for fld_name, fld in zip(fld_names, fields):
            self[fld_name] = fld
        return self[name]

    @property
    def geom(self):
        """Header info from bin file"""
        if self._header is UNDETERMINED:
            self._header = None
            for par in self.step.sdat.scan:
                fieldfile = self.step.sdat.filename(par, self.step.isnap)
                header = stagyyparsers.fields(fieldfile, only_header=True)
                if header is not None:
                    self._header = header
                    break
        if self._geom is UNDETERMINED:
            self._geom = _Geometry(self._header, self.step.sdat.par)
        return self._geom


class _Indexes:

    """Efficient isnap -> istep correspondance"""

    def __init__(self):
        """Indexes lie in 400 lists of <=250 tuples"""
        self._nlst = 400
        self.isnap = [[] for _ in range(self._nlst)]
        self.istep = [[] for _ in range(self._nlst)]

    def _isnap_idx(self, isnap):
        """Return super_idx, idx and if isnap already present"""
        super_idx = isnap // (100000 // self._nlst)
        idx = bisect.bisect_left(self.isnap[super_idx], isnap)
        return super_idx, idx, (idx < len(self.isnap[super_idx]) and
                                self.isnap[super_idx][idx] == isnap)

    def get_istep(self, isnap):
        """Return istep corresponding to isnap"""
        super_idx, idx, present = self._isnap_idx(isnap)
        if present:
            return self.istep[super_idx][idx]
        else:
            return UNDETERMINED

    def insert(self, isnap, istep):
        """Inject isnap, istep correspondance"""
        super_idx, idx, present = self._isnap_idx(isnap)
        if not present:
            self.isnap[super_idx].insert(idx, isnap)
            self.istep[super_idx].insert(idx, istep)


class _Step:

    """Time step data structure"""

    def __init__(self, istep, sdat):
        self.istep = istep
        self.sdat = sdat
        self.fields = _Fields(self)
        self._isnap = UNDETERMINED
        self._irsnap = UNDETERMINED
        self._itsnap = UNDETERMINED

    @property
    def geom(self):
        """Geometry object"""
        return self.fields.geom

    @property
    def timeinfo(self):
        """Relevant time series data"""
        if self.itsnap is None:
            return None
        else:
            return self.sdat.tseries[self.itsnap]

    @property
    def rprof(self):
        """Relevant radial profiles data"""
        if self.irsnap is None:
            return None
        else:
            return self.sdat.rprof[self.irsnap]

    @property
    def isnap(self):
        """Fields snap corresponding to time step"""
        if self._isnap is UNDETERMINED:
            istep = None
            isnap = -1
            # could be more efficient if do 0 and -1 then bisection
            # (but loose intermediate <- would probably use too much
            # memory for what it's worth if search algo is efficient)
            while (istep is None or istep < self.istep) and isnap < 99999:
                isnap += 1
                istep = self.sdat.snaps[isnap].istep
                self.sdat.snaps.bind(isnap, istep)
                # all intermediate istep could have their ._isnap to None
            if istep != self.istep:
                self._isnap = None
        return self._isnap

    @isnap.setter
    def isnap(self, isnap):
        """Fields snap corresponding to time step"""
        try:
            self._isnap = int(isnap)
        except ValueError:
            pass

    @property
    def irsnap(self):
        """Radial snap corresponding to time step"""
        self.sdat.rprof
        if self._irsnap is UNDETERMINED:
            self._irsnap = None
        return self._irsnap

    @irsnap.setter
    def irsnap(self, irsnap):
        """Radial snap corresponding to time step"""
        try:
            self._irsnap = int(irsnap)
        except ValueError:
            pass

    @property
    def itsnap(self):
        """Time info entry corresponding to time step"""
        self.sdat.tseries
        if self._itsnap is UNDETERMINED:
            self._itsnap = None
        return self._itsnap

    @itsnap.setter
    def itsnap(self, itsnap):
        """Time info entry corresponding to time step"""
        try:
            self._itsnap = int(itsnap)
        except ValueError:
            pass


class _EmptyStep(_Step):

    """Dummy step object for nonexistent snaps"""

    def __init__(self):
        super().__init__(None, None)

    def __getattribute__(self, name):
        return None


class _Steps(dict):

    """Implement the .steps[istep] accessor"""

    def __init__(self, sdat):
        self.sdat = sdat
        super().__init__()

    def __setitem__(self, key, value):
        pass

    def __getitem__(self, key):
        try:
            # slice
            start = key.start or 0
            stop = key.stop  # or -1
            step = key.step or 1
            return (super(self.__class__, self).__getitem__(k)
                    for k in range(start, stop, step))
        except AttributeError:
            return super().__getitem__(key)

    def __missing__(self, istep):
        if istep is None:  # if called for nonexistent snap
            return _EmptyStep()
        try:
            istep = int(istep)
        except ValueError:
            raise ValueError('Time step should be an integer value')
        if istep < 0:  # need handling of negative num
            raise ValueError('Time step should be positive')
        if not self.__contains__(istep):
            super().__setitem__(istep, _Step(istep, self.sdat))
        return super().__getitem__(istep)


class _Snaps(_Steps):

    """Implement the .snaps[isnap] accessor"""

    def __init__(self, sdat):
        self._isteps = _Indexes()
        super().__init__(sdat)

    def __getitem__(self, key):
        try:
            # slice
            start = key.start or 0
            stop = key.stop  # or -1
            step = key.step or 1
            return (self.__missing__(k)
                    for k in range(start, stop, step))
        except AttributeError:
            return self.__missing__(key)

    def __missing__(self, isnap):
        istep = self._isteps.get_istep(isnap)  # need handling of negative num
        if istep is UNDETERMINED:
            for par in self.sdat.scan:
                fieldfile = self.sdat.filename(par, isnap)
                istep = stagyyparsers.fields(fieldfile, only_istep=True)
                if istep is not None:
                    self.bind(isnap, istep)
                    return self.sdat.steps[istep]
            self._isteps.insert(isnap, None)
        return self.sdat.steps[istep]

    def bind(self, isnap, istep):
        """Make the isnap <-> istep link"""
        self._isteps.insert(isnap, istep)
        self.sdat.steps[istep].isnap = isnap


class StagyyData:

    """Offer a generic interface to StagYY output data"""

    def __init__(self, args):
        """Generic lazy StagYY output data accessors"""
        # currently, args used to find name of files.
        # This module should be independant of args.
        # Only parameters should be path and par_dflt,
        #    and par file would be read here.
        # `name` option of core_parser should be
        #    removed since it is useless if HDF5
        #
        # User-end: dealing with isolated files should
        # be done by creating a dummy par file instead
        # of using command line options
        self.args = args
        self.par = args.par_nml
        self.scan = set.intersection(
            set(args.scan.split(',')),
            set(item.par for item in constants.FIELD_VAR_LIST.values()))
        self.steps = _Steps(self)
        self.snaps = _Snaps(self)
        self._tseries = UNDETERMINED
        self._rprof = UNDETERMINED

    @property
    def tseries(self):
        """Time series data"""
        if self._tseries is UNDETERMINED:
            timefile = self.filename('time.dat')
            self._tseries = stagyyparsers.time_series(timefile)
            for itsnap, timeinfo in enumerate(self._tseries):
                istep = int(timeinfo[0])
                self.steps[istep].itsnap = itsnap
        return self._tseries

    @property
    def rprof(self):
        """Radial profiles data"""
        if self._rprof is UNDETERMINED:
            rproffile = self.filename('rprof.dat')
            rprof_data = stagyyparsers.rprof(rproffile)
            isteps = []
            times = []
            data = []
            for irsnap, (istep, time, prof) in enumerate(rprof_data):
                self.steps[istep].irsnap = irsnap
                times.append(time)
                isteps.append(istep)
                data.append(prof)
            self._rprof = _Rprof(data, times, isteps)
        return self._rprof

    def filename(self, fname, timestep=None, suffix=''):
        """return name of StagYY out file"""
        # remove stag_file from misc, also _file_name and lastfile
        if timestep is not None:
            fname += '{:05d}'.format(timestep)
        fname = os.path.join(self.args.path,
                             self.args.name + '_' + fname + suffix)
        return fname