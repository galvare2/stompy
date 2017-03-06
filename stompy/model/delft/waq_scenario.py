from __future__ import print_function

import os
import re
import textwrap

import glob
import sys
import subprocess
import shutil
import datetime
import numpy as np
import numpy.lib.recfunctions as rfn
from scipy import sparse 
from ... import filters
from ... import utils
forwardTo=utils.forwardTo
import logging

import time
import pandas as pd
from matplotlib.dates import num2date,date2num
import matplotlib.pyplot as plt
from itertools import count
from six import iteritems
import six # next

try:
    from ...spatial import wkb2shp
except ImportError:
    print("wkb2shp not found - not loading/saving of shapefiles")

from shapely import geometry
try:
    from shapely.ops import cascaded_union
except ImportError:
    cascaded_union=None

from collections import defaultdict,OrderedDict,Iterable
import scipy.spatial

from  ... import scriptable
from ...io import qnc
import xarray as xr
from ...grid import unstructured_grid
from ...grid import ugrid
import threading

from . import nefis
from . import nefis_nc
from . import waq_process
from . import process_diagram

def waq_timestep_to_timedelta(s):
    d,h,m,secs = [int(x) for x in [s[:-6],s[-6:-4],s[-4:-2],s[-2:]] ]
    return datetime.timedelta(days=d,hours=h,minutes=m,seconds=secs)
def timedelta_to_waq_timestep(td):
    total_seconds=td.total_seconds()
    assert td.microseconds==0
    secs=total_seconds % 60
    mins=(total_seconds // 60) % 60
    hours=(total_seconds // 3600) % 24
    days=(total_seconds // 86400) 
    
    #                                       seconds
    #                                     minutes
    #                                   hours
    #                                days 
    # hydrodynamic-timestep    '00000000 00 3000'
    
    return "%08d%02d%02d%02d"%(days,hours,mins,secs)

def rel_symlink(src,dst):
    """ Create a symlink, adjusting for a src path relative
    to cwd rather than the directory of dst. 
    """
    # given two paths that are either absolute or relative to
    # pwd, create a symlink that includes the right number of
    # ../..'s
    if os.path.isabs(src): # no worries
        os.symlink(src,dst)
    else:
        pre = os.path.relpath(os.path.dirname(src),os.path.dirname(dst))
        os.symlink( os.path.join(pre,os.path.basename(src)), dst )


# Classes used in defining a water quality model scenario

CLOSED=0
BOUNDARY='boundary'

def tokenize(fp,comment=';'):
    """ tokenize waq inputs, handling comments, possibly include.
    no casting.
    """
    for line in fp:
        items = line.split(comment)[0].strip().split()
        for tok in items:
            yield tok

            
class MonTail(object):
    def __init__(self,mon_fn,log=None,sim_time_seconds=None):
        """ 
        mon_fn: path to delwaq2 monitor file
        log: logging object to which messages are sent via .info()
        sim_time_seconds: length of simulation, for calculation of relative speed
        """
        self.signal_stop=False
        self.sim_time_seconds=sim_time_seconds
        self.log=log
        if 1:
            self.thr=threading.Thread(target=self.tail,args=[mon_fn])
            self.thr.daemon=True
            self.thr.start()
        else:
            self.tail(mon_fn)
    def stop(self):
        self.signal_stop=True
        self.thr.join()

    def msg(self,s):
        if self.log is None:
            print(s)
        else:
            # can be annoying to get info to print, but less alarming than constantly
            # seeing warnings
            self.log.info(s)

    def tail(self,mon_fn):
        # We may have to wait for the file to exist...
        if not os.path.exists(mon_fn):
            self.msg("Waiting for %s to be created"%mon_fn)
            while not os.path.exists(mon_fn):
                if self.signal_stop:
                    self.msg("Got the signal to stop, but never saw file")
                    return
                time.sleep(0.1)
            self.msg("Okay - %s exists now"%mon_fn)

        # There is still the danger that the file changes size..
        sample_pcts=[]
        sample_secs=[]
        with open(mon_fn) as fp:
            # First, get up to the last line:
            last_line=""
            while not self.signal_stop:
                next_line=fp.readline()
                if next_line=='':
                    break
                last_line=next_line

            # and begin to tail:
            while not self.signal_stop:
                next_line=fp.readline()
                if next_line=='':
                    # a punt on cases where the file has been truncated.
                    fp.seek(0,2) # seek to end
                    time.sleep(0.1)
                else:
                    last_line=next_line
                    if 'Completed' in last_line:
                        self.msg(last_line.strip())
                        try:
                            pct_complete=float(re.match(r'([0-9\.]+).*',last_line.strip()).group(1))
                            sample_pcts.append(pct_complete)
                            sample_secs.append(time.time())
                            if len(sample_pcts)>1:
                                # pct/sec
                                mb=np.polyfit(sample_secs,sample_pcts,1)
                                if mb[0] != 0:
                                    time_remaining=(100 - sample_pcts[-1]) / mb[0]
                                    # mb[0] has units pct/sec
                                    etf=datetime.datetime.now() + datetime.timedelta(seconds=time_remaining)
                                    if self.sim_time_seconds is not None:
                                        speed=self.sim_time_seconds*mb[0]/100.0
                                        speed="%.2fx realtime"%speed
                                    else:
                                        speed="n/a"
                                    self.msg("Time remaining: %.3fh (%s) %s"%(time_remaining/3600.,
                                                                              etf.strftime('%c'),
                                                                              speed))
                        except Exception as exc:
                            # please, just don't let this stupid thing stop the process
                            print(exc)


class WaqException(Exception):
    pass

class Hydro(object):
    time0=None # a datetime instance, for the *reference* time.
    # note that a hydro object may start at some nonzero offset 
    # from this reference time.

    @property
    def fn_base(self): # base filename for output. typically com-<scenario name>
        return 'com-{}'.format(self.scenario.name)

    t_secs=None # timesteps in seconds from time0 as 'i4'

    scenario=None

    # constants:
    CLOSED=CLOSED
    BOUNDARY=BOUNDARY


    def __init__(self,**kws):
        self.log=logging.getLogger(self.__class__.__name__)

        for k,v in kws.items():
            # if k in self.__dict__ or k in self.__class__.__dict__:
            #     self.__dict__[k]=v
            # else:
            #     raise Exception("Unknown keyword option: %s=%s"%(k,v))
            try:
                getattr(self,k)
                setattr(self,k,v)
            except AttributeError:
                raise Exception("Unknown keyword option: %s=%s"%(k,v))

    @property
    def t_dn(self):
        """ convert self.time0 and self.t_secs to datenums
        """
        from matplotlib.dates import num2date,date2num
        return date2num(self.time0) + self.t_secs/86400.

    @property
    def time_step(self):
        """ Return an integer in DelWAQ format for the time step.
        i.e. ddhhmmss.  Assumes that scu is 1s.
        """
        dt_secs=np.diff(self.t_secs)
        dt_sec=dt_secs[0]
        assert np.all( dt_sec==dt_secs )

        rest,seconds=divmod(dt_sec,60)
        rest,minutes=divmod(rest,60)
        days,hours=divmod(rest,24)
        return ((days*100 + hours)*100 + minutes)*100 + seconds

    # num_exch => use n_exch
    @property
    def n_exch(self):
        # unstructured would have num_exch_y==0
        return self.n_exch_x + self.n_exch_y + self.n_exch_z

    # num_seg => use n_seg
    n_seg=None # overridden in subclass or explicitly set.
    
    def areas(self,t):
        """ returns ~ np.zeros(self.n_exch,'f4'), for the timestep given by time t
        specified in seconds from time0.  areas in m2.
        """
        raise WaqException("Implement in subclass")

    def flows(self,t):
        """ returns flow rates ~ np.zeros(self.n_exch,'f4'), for given timestep.
        flows in m3/s.
        """
        raise WaqException("Implement in subclass")

    @property
    def scen_t_secs(self):
        """
        the subset of self.t_secs needed for the scenario's timeline
        this is the subset of times typically used when self.write() is called
        """
        hydro_datetimes=self.t_secs*self.scenario.scu + self.time0 
        start_i,stop_i=np.searchsorted(hydro_datetimes,
                                       [self.scenario.start_time,
                                        self.scenario.stop_time])
        if start_i>0:
            start_i-=1
        if stop_i < len(self.t_secs):
            stop_i+=1
        return self.t_secs[start_i:stop_i]

    @property 
    def are_filename(self):
        return os.path.join(self.scenario.base_path,self.fn_base+".are")
        
    def write_are(self):
        """
        Write are file
        """
        with open(self.are_filename,'wb') as fp:
            for t_sec in self.scen_t_secs.astype('i4'):
                fp.write(t_sec.tobytes()) # write timestamp
                fp.write(self.areas(t_sec).astype('f4').tobytes())

    @property
    def flo_filename(self):
        return os.path.join(self.scenario.base_path,self.fn_base+".flo")

    def write_flo(self):
        """
        Write flo file
        """
        with open(self.flo_filename,'wb') as fp:
            for t_sec in self.scen_t_secs.astype('i4'):
                fp.write(t_sec.tobytes()) # write timestamp
                fp.write(self.flows(t_sec).astype('f4').tobytes())

    def seg_attrs(self,number):
        """ 
        1: active/inactive
          defaults to all active
        2: top/mid/bottom
          inferred from results of infer_2d_elements()
        """
        if number==1:
            # default, all active. may need to change this?
            return np.ones(self.n_seg,'i4')
        if number==2:
            self.infer_2d_elements()

            # 0: single layer, 1: surface, 2: mid-water column, 3: bed
            attrs=np.zeros(self.n_seg,'i4')

            for elt_i,sel in utils.enumerate_groups(self.seg_to_2d_element):
                if elt_i<0:
                    continue # inactive segments

                # need a different code if it's a single layer
                if len(sel)>1:
                    attrs[sel[0]]=1
                    attrs[sel[1:-1]]=2
                    attrs[sel[-1]]=3
                else:
                    attrs[sel[0]]=0 # top and bottom
            return attrs

    def text_atr(self):
        """ This used to return just the single number prefix (1 for all segs, no defaults)
        and the per-seg values.  Now it returns the entire attribute section, including 
        constant and time-varying.  No support yet for time-varying attributes, though.
        """

        # grab the values to find out which need to be written out.
        attrs1=self.seg_attrs(number=1)
        attrs2=self.seg_attrs(number=2)

        lines=[]
        count=0
        if np.any(attrs1!=1): # departs from default
            count+=1
            lines+=["1 1 1 ; num items, feature, input here",
                    "    1 ; all segs, without defaults",
                    "\n".join([str(a) for a in attrs1])]
        if np.any(attrs2!=0): # departs from default
            count+=1
            lines+=["1 2 1 ; num items, feature, input here",
                    "    1 ; all segs, without defaults",
                    "\n".join([str(a) for a in attrs2])]
        lines[:0]=["%d ; count of time-independent contributions"%count]
        lines.append(" 0    ; no time-dependent contributions")

        return "\n".join(lines)
                
    def write_atr(self):
        """
        write atr file
        """
        # might need to change with z-level aggregation
        with open(os.path.join(self.scenario.base_path,self.fn_base+".atr"),'wt') as fp:
            fp.write(self.text_atr())

    # lengths from src segment to face, face to destination segment. 
    # in order of directions - x first, y second, z third.
    # [n_exch,2]*'f4' 
    exchange_lengths=None
    def write_len(self):
        """
        write len file
        """
        with open(os.path.join(self.scenario.base_path,self.fn_base+".len"),'wb') as fp:
            fp.write( np.array(self.n_exch,'i4').tobytes() )
            fp.write(self.exchange_lengths.astype('f4').tobytes())

    # like np.zeros( (n_exch,4),'i4')
    # 2nd dimension is upwind, downwind, up-upwind, down-downwind
    # N.B. this format is likely specific to structured hydro
    pointers=None 

    def write_poi(self):
        """
        write poi file
        """
        with open(os.path.join(self.scenario.base_path,self.fn_base+".poi"),'wb') as fp:
            fp.write(self.pointers.astype('i4').tobytes())

    def volumes(self,t):
        """ segment volumes in m3, [n_seg]*'f4'
        """
        raise WaqException("Implement in subclass")

    @property
    def vol_filename(self):
        return os.path.join(self.scenario.base_path,self.fn_base+".vol")

    def write_vol(self):
        """ write vol file
        """
        with open(self.vol_filename,'wb') as fp:
            for t_sec in self.scen_t_secs.astype('i4'):
                fp.write(t_sec.tobytes()) # write timestamp
                fp.write(self.volumes(t_sec).astype('f4').tobytes())

    def vert_diffs(self,t):
        """ returns [n_segs]*'f4' vertical diffusivities in m2/s
        """
        raise WaqException("Implement in subclass")

    flowgeom_filename='flowgeom.nc'
    def write_geom(self):
        ds=self.get_geom()
        if ds is None:
            self.log.debug("This Hydro class does not support writing geometry")
            
        dest=os.path.join(self.scenario.base_path,
                          self.flowgeom_filename)
        ds.to_netcdf(dest)
    def get_geom(self):
        # Return the geometry as an xarray / ugrid-ish Dataset.
        return None

    # How is the vertical handled in the grid?
    # affects outputting ZMODEL NOLAY in the inp file
    VERT_UNKNOWN=0
    ZLAYER=1
    SIGMA=2
    SINGLE=3
    @property
    def vertical(self):
        geom=self.get_geom()
        if geom is None:
            return self.VERT_UNKNOWN
        for v in geom.variables:
            standard_name=geom[v].attrs.get('standard_name',None)
            if standard_name == 'ocean_sigma_coordinate':
                return self.SIGMA
            if standard_name == 'ocean_zlevel_coordinate':
                return self.ZLAYER
        return self.VERT_UNKNOWN
    
    def grid(self):
        """ if possible, return an UnstructuredGrid instance for the 2D 
        layout.  returns None if the information is not available.
        """
        return None

    _params=None
    # force had been false, but it doesn't play well with running multiple
    # scenarios with the same hydro.
    def parameters(self,force=True):
        if force or (self._params is None):
            hyd=NamedObjects(scenario=self.scenario)
            self._params = self.add_parameters(hyd)
        return self._params
        
    def add_parameters(self,hyd):
        """ Moved from waq_scenario init_hydro_parameters
        """
        self.log.debug("Adding planform areas parameter")
        hyd['SURF']=self.planform_areas()
        
        try:
            self.log.debug("Adding bottom depths parameter")
            hyd['bottomdept']=self.bottom_depths()
        except NotImplementedError:
            self.log.info("Bottom depths will be inferred")

        self.log.debug("Adding VertDisper parameter")
        hyd['VertDisper']=ParameterSpatioTemporal(func_t=self.vert_diffs,
                                                  times=self.t_secs,
                                                  hydro=self)
        self.log.debug("Adding depths parameter")
        try:
            hyd['DEPTH']=self.depths()
        except NotImplementedError:
            self.log.info("Segment depth will be inferred")
        return hyd

    def write(self):
        self.log.debug('Writing 2d links')
        self.write_2d_links()
        self.log.debug('Writing boundary links')
        self.write_boundary_links()
        self.log.debug('Writing attributes')
        self.write_atr()
        self.log.info('Writing hyd file')
        self.write_hyd()
        self.log.info('Writing srf file')
        self.write_srf()
        self.log.info('Writing hydro parameters')
        self.write_parameters()
        self.log.debug('Writing geom')
        self.write_geom()
        self.log.debug('Writing areas')
        self.write_are()
        self.log.debug('Writing flows')
        self.write_flo()
        self.log.debug('Writing lengths')
        self.write_len()
        self.log.debug('Writing pointers')
        self.write_poi()
        self.log.debug('Writing volumes')
        self.write_vol()

    def write_srf(self):
        if 0: # old Hydro behavior:
            self.log.info("No srf to write")
        else:
            try:
                plan_areas=self.planform_areas()
            except WaqException as exc:
                self.log.warning("No planform areas to write")
                return
            
            self.infer_2d_elements()
            nelt=self.n_2d_elements
            
            # painful breaking of abstraction.
            if isinstance(plan_areas,ParameterSpatioTemporal):
                surfaces=plan_areas.evaluate(t=0).data
            elif isinstance(plan_areas,ParameterSpatial):
                surfaces=plan_areas.data
            elif isinstance(plan_areas,ParameterConstant):
                surfaces=plan_areas.value * np.ones(nelt,'f4')
            elif isinstance(plan_areas,ParameterTemporal):
                surfaces=plan_areas.values[0] * np.ones(nelt,'f4')
            else:
                raise Exception("plan areas is %s - unhandled"%(str(plan_areas)))

            # this needs to be in sync with what write_hyd writes, and
            # the supporting_file statement in the hydro_parameters
            fn=os.path.join(self.scenario.base_path,self.surf_filename)
            
            with open(fn,'wb') as fp:
                # shape, shape, count, x,x,x according to waqfil.m
                hdr=np.zeros(6,'i4')
                hdr[0]=hdr[2]=hdr[3]=hdr[4]=nelt
                hdr[1]=1
                hdr[5]=0
                fp.write(hdr.tobytes())
                fp.write(surfaces.astype('f4'))
        
    def write_parameters(self):
        # parameters are updated with force=True on Scenario instantiation,
        # don't need to do it here.
        for param in self.parameters(force=False).values():
            # don't care about the textual description
            _=param.text(write_supporting=True)
    def planform_areas(self):
        """
        return Parameter, typically ParameterSpatial( Nsegs * 'f4' )
        """
        raise WaqException("Implement in subclass")

    def depths(self):
        raise NotImplementedError("This class does not directly provide depth")

    def bottom_depths(self):
        """ 
        return Parameter, typically ParameterSpatial( Nsegs * 'f4' )
        """
        raise NotImplementedError("Implement in subclass")

    def seg_active(self):
        # this is now just a thin wrapper on seg_attrs
        return self.seg_attrs(number=1).astype('b1')

    seg_to_2d_element=None
    seg_k=None
    n_2d_elements=0
    def infer_2d_elements(self):
        """
        populates seg_to_2d_element: [n_seg] 0-based indices,
        mapping each segment to its 0-based 2d element
        Also populates self.seg_k as record of vertical layers of segments.
        inactive segments are not assigned any of these.
        """
        if self.seg_to_2d_element is None:
            n_2d_elements=0
            seg_to_2d=np.zeros(self.n_seg,'i4')-1 # 0-based segment => 0-based 2d element.
            # 0-based layer, k=0 is surface
            # accuracy seg_k depends on prismatic topology of cells
            seg_k=np.zeros(self.n_seg,'i4')-1 

            poi=self.pointers
            poi_vert=poi[-self.n_exch_z:]

            # don't make any assumptions about layout -
            # but by enumerating 2D segments in the same order as the
            # first segments, should preserve ordering from the top layer.

            # really this should use self.seg_to_exchs, so that we don't
            # duplicate preprocessing.  Another day.
            # preprocess neighbor queries:
            nbr_up=defaultdict(list) 
            nbr_down=defaultdict(list)

            self.log.debug("Inferring 2D elements, preprocess adjacency")

            # all 0-based
            for seg_from,seg_to in (poi_vert[:,:2] - 1):
                nbr_up[seg_to].append(seg_from)
                nbr_down[seg_from].append(seg_to)

            seg_active=self.seg_active()

            for seg in range(self.n_seg): # 0-based segment
                if seg%50000==0:
                    self.log.info("Inferring 2D elements, %d / %d 3-D segments"%(seg,self.n_seg))

                if not seg_active[seg]:
                    continue

                def trav(seg,elt,k):
                    # mark this segment as being from 2d element elt,
                    # and mark any unmarked segments vertically adjacent
                    # with the same element.
                    # returns the number segments marked
                    if seg_to_2d[seg]>=0:
                        return 0 # already marked
                    seg_to_2d[seg]=elt
                    seg_k[seg]=k
                    count=1

                    # would like to check departures from the standard
                    # format where (i) vertical exchanges are upper->lower,
                    # and all of the top-layer segments come first.

                    v_nbrs1=nbr_down[seg]
                    v_nbrs2=nbr_up[seg]
                    v_nbrs=v_nbrs1+v_nbrs2

                    # extra check on conventions
                    for nbr in v_nbrs2: # a segment 'above' us
                        if nbr>0 and seg_to_2d[nbr]<0:
                            # not a boundary, and not visited
                            self.log.warning("infer_2d_elements: spurious segments on top.  segment ordering may be off")

                    for nbr in v_nbrs1:
                        if nbr>=0: # not a boundary
                            count+=trav(nbr,elt,k+1)
                    for nbr in v_nbrs2: 
                        # really shouldn't hit any segments this way
                        if nbr>=0:
                            count+=trav(nbr,elt,k-1)
                    return count

                if trav(seg,n_2d_elements,k=0):
                    # print("traverse from seg=%d incrementing n_2d_elements from %d"%(seg,n_2d_elements))
                    n_2d_elements+=1
            self.n_2d_elements=n_2d_elements
            self.seg_to_2d_element=seg_to_2d
            self.seg_k=seg_k
        return self.seg_to_2d_element

    def extrude_element_to_segment(self,V):
        """ V: [n_2d_elements] array
        returns [n_seg] array
        """
        self.infer_2d_elements()
        return V[self.seg_to_2d_element]


    # hash of segment id (0-based) to list of exchanges
    # order by horizontal, decreasing z, then vertical, decreasing z.
    _seg_to_exchs=None 
    def seg_to_exchs(self,seg):
        if self._seg_to_exchs is None:
            self._seg_to_exchs=ste=defaultdict(list)
            for exch,(s_from,s_to,dumb,dumber) in enumerate(self.pointers):
                ste[s_from-1].append(exch)
                ste[s_to-1].append(exch)
        return self._seg_to_exchs[seg]
            
    def seg_to_exch_z(self,preference='lower'):
        """ Map 3D segments to an associated vertical exchange
        (i.e. for getting areas)
        preference=='lower': will give preference to an exchange lower down in the watercolum
        NB: if a water column has only one layer, the corresponding 
        exch index will be set to -1.
        """
        nz=self.n_exch-self.n_exch_z # first index of vert. exchanges
        
        seg_z_exch=np.zeros(self.n_seg,'i4')
        pointers=self.pointers
        
        warned=False

        for seg in range(self.n_seg):
            # used to have a filter expression here, but that got weird in Py3k.
            vert_exchs=[s for s in self.seg_to_exchs(seg) if s>=nz]

            if len(vert_exchs)==0:
                # dicey! some callers may not expect this.
                if not warned:
                    self.log.warning("seg %d has 1 layer - no z exch"%seg)
                    self.log.warning("further warnings suppressed")
                    warned=True
                vert_exch=-1
            elif preference=='lower':
                vert_exch=vert_exchs[-1] 
            elif preference=='upper':
                vert_exch=vert_exchs[0]
            else:
                raise ValueError("Bad preference value: %s"%preference)
            seg_z_exch[seg]=vert_exch
        return seg_z_exch

    def check_volume_conservation_nonincr(self):
        """
        Compare time series of segment volumes to the integration of 
        fluxes.  This version loads basically everything into RAM,
        so should only be used with very simple models.
        """
        flows=[] 
        volumes=[]

        # go through the time variation, see if we can show that volume
        # is conserved
        print("Loading full period, aggregated flows and volumes")
        for ti,t in enumerate(self.t_secs):
            sys.stdout.write('.') ; sys.stdout.flush()
            if (ti+1)%50==0:
                print()

            flows.append( self.flows(t) )
            volumes.append( self.volumes(t) )
        print()
        flows=np.array(flows)
        volumes=np.array(volumes)

        print("Relative error in volume conservation.  Expect 1e-6 with 32-bit floats")
        for seg in range(self.n_agg_segments):
            seg_weight=np.zeros( self.n_exch )
            seg_weight[ self.agg_exch['from']==seg ] = -1
            seg_weight[ self.agg_exch['to']==seg ] = 1

            seg_Q=np.dot(seg_weight,flows.T)
            dt=np.diff(self.t_secs)
            seg_dV=seg_Q*np.median(dt)

            pred_V=volumes[0,seg]+np.cumsum(seg_dV[:-1])
            err=volumes[1:,seg] - pred_V
            rel_err=err / volumes[:,seg].mean()
            rmse=np.sqrt( np.mean( rel_err**2 ) )
            print(rmse)

    _QtodV=None
    _QtodVabs=None
    def mats_QtodV(self):
        # refactored out of check_volume_conservation_incr()
        if self._QtodV is None:
            # build a sparse matrix for mapping exchange flux to segments
            # QtodV.dot(Q): rows of QtodV correspond to segment
            # columns correspond to exchanges
            rows=[]
            cols=[]
            vals=[]

            for exch_i,(seg_from,seg_to) in enumerate(self.pointers[:,:2]):
                if seg_from>0:
                    rows.append(seg_from-1)
                    cols.append(exch_i)
                    vals.append(-1.0)
                if seg_to>0:
                    rows.append(seg_to-1)
                    cols.append(exch_i)
                    vals.append(1.0)

            QtodV=sparse.coo_matrix( (vals, (rows,cols)),
                                     (self.n_seg,self.n_exch) )
            QtodVabs=sparse.coo_matrix( (np.abs(vals), (rows,cols)),
                                        (self.n_seg,self.n_exch) )
            self._QtodV = QtodV
            self._QtodVabs=QtodVabs
        return self._QtodV,self._QtodVabs

    def check_volume_conservation_incr(self,seg_select=slice(None),
                                       err_callback=None):
        """
        Compare time series of segment volumes to the integration of 
        fluxes.  This version loads just two timesteps at a time,
        and also includes some more generous checks on how well the
        fluxes close.

        seg_select: an slice or bitmask to select which segments are
        included in error calculations.  Use this to omit ghost segments, 
        for instance.

        err_callback(time_index,rel_errors): called for each interval
         checked, with the index (1...) and relative error per segment.
        """
        t_secs=self.t_secs

        QtodV,QtodVabs = self.mats_QtodV()

        try:
            plan_areas=self.planform_areas()
        except FileNotFoundError:
            plan_areas=None

        for ti,t in enumerate(t_secs):
            Vnow=self.volumes(t)

            if plan_areas is not None:
                seg_plan_areas=plan_areas.evaluate(t=t).data
            else:
                seg_plan_areas=None

            if ti>0:
                dt=t_secs[ti]-t_secs[ti-1]
                dVmag=QtodVabs.dot(np.abs(Qlast)*dt)
                Vpred=Vlast + QtodV.dot(Qlast)*dt

                err=Vnow - Vpred
                valid=(Vnow+dVmag)!=0.0
                # rel_err=np.abs(err) / (Vnow+dVmag)
                rel_err=np.zeros(len(err),'f8')
                rel_err[valid]=np.abs(err[valid])/(Vnow+dVmag)[valid]
                rel_err[~valid] = np.abs(err[~valid])

                # report the error in terms of thickness, to normalize
                # for area and report a dz which represents the absolute
                # error
                # if seg_plan_areas is not None:
                #     z_err=err[valid] / seg_plan_areas[valid]
                # else:
                #     z_err=None
                    
                if err_callback:
                    err_callback(ti,rel_err)

                rmse=np.sqrt( np.mean( rel_err[seg_select]**2 ) )
                if rel_err[seg_select].max() > 1e-4:
                    self.log.warning("****************BAD Volume Conservation*************")
                    self.log.warning("  t=%10d   RMSE: %e    Max rel. err: %e"%(t,rmse,rel_err[seg_select].max()))
                    self.log.warning("  %d segments above rel err tolerance"%np.sum( rel_err[seg_select]>1e-4 ))
                    self.log.info("Bad segments: %s"%( np.nonzero( rel_err[seg_select]>1e-4 )[0] ) )

                    bad_seg=np.arange(len(rel_err))[seg_select][np.argmax(rel_err[seg_select])]
                    self.log.warning("  Worst segment is index %d"%bad_seg)
                    self.log.warning("  Vlast=%f  Vpred=%f  Vnow=%f"%(Vlast[bad_seg],
                                                         Vpred[bad_seg],
                                                         Vnow[bad_seg]))
                    if seg_plan_areas is not None:
                        self.log.warning("  z error=%f m"%( err[bad_seg] / seg_plan_areas[bad_seg] ))
                    Vin =dt*Qlast[ self.pointers[:,1] == bad_seg+1 ]
                    Vout=dt*Qlast[ self.pointers[:,0] == bad_seg+1 ]
                    bad_Q=np.concatenate( [Vin,-Vout] )
                    Qmag=np.max(np.abs(bad_Q))
                    self.log.warning("  Condition of dV: Qmag=%f Qnet=%f mag/net=%f"%(Qmag,bad_Q.sum(),Qmag/bad_Q.sum()))

            Qlast=self.flows(t)
            Vlast=Vnow

    # Boundary handling
    # this representation follows the naming in the input file
    boundary_dtype=[('id','S20'),
                    ('name','S20'),
                    ('type','S20')]
    @property
    def n_boundaries(self):
        return -self.pointers[:,:2].min()

    # for a while, was using 'grouped', but that relies on boundary segments,
    # which didn't transfer well to aggregated domains which usually have many
    # unaggregated links going into the same aggregated element.
    boundary_scheme='lgrouped'
    def boundary_defs(self):
        """ Generic boundary defs - types default to ids
        """
        Nbdry=self.n_boundaries
        
        bdefs=np.zeros(Nbdry, self.boundary_dtype)
        for i in range(Nbdry):
            bdefs['id'][i]="boundary %d"%(i+1)
        bdefs['name']=bdefs['id']
        if self.boundary_scheme=='id':
            bdefs['type']=bdefs['id']
        elif self.boundary_scheme in ['element','grouped']:
            bc_segs=self.bc_segs() 
            self.infer_2d_elements()
            if self.boundary_scheme=='element':
                bdefs['type']=["element %d"%( self.seg_to_2d_element[seg] )
                               for seg in bc_segs]
            elif self.boundary_scheme=='grouped':
                bc_groups=self.group_boundary_elements()
                bdefs['type']=[ bc_groups['name'][self.seg_to_2d_element[seg]] 
                                for seg in bc_segs]
        elif self.boundary_scheme == 'lgrouped':
            bc_lgroups=self.group_boundary_links()
            bc_exchs=np.nonzero(self.pointers[:,0]<0)[0]
            self.infer_2d_links()
            bdefs['type']=[ bc_lgroups['name'][self.exch_to_2d_link[exch]] 
                            for exch in bc_exchs]
        else:
            raise ValueError("Boundary scheme is bad: %s"%self.boundary_scheme)
        return bdefs

    def bc_segs(self):
        # Return an array of segments (0-based) corresponding to the receiving side of
        # the boundary exchanges
        poi=self.pointers
        # need to associate an internal segment with each bc exchange
        bc_exch=(poi[:,0]<0)
        bc_external=-1-poi[bc_exch,0]
        assert bc_external[0]==0
        assert np.all( np.diff(bc_external)==1)
        return poi[bc_exch,1]-1

    def time_to_index(self,t):
        return np.searchsorted(self.t_secs,t).clip(0,len(self.t_secs)-1)

    # not really that universal, but moving towards a common
    # data structure which includes names for elements, in 
    # which case this maps element names to indexes
    def coerce_to_element_index(self,x,return_boundary_name=True): 
        if isinstance(x,str):
            try:
                x=np.nonzero( self.elements['name']==x )[0][0]
            except IndexError:
                if return_boundary_name:
                    # probably got a string that's the name of a boundary
                    return x
                else:
                    raise
        return x

    def write_hyd(self,fn=None):
        """ Write an approximation to the hyd file output by D-Flow FM
        for consumption by delwaq

        DwaqAggregator has a good implementation, but with some
        specialization which would need to be factored out for here.

        That implementation has been copied here, and is in the process
        of being fixed to more general usage.

        Write an approximation to the hyd file output by D-Flow FM
        for consumption by delwaq or HydroFiles
        respects scen_t_secs
        """
        # currently the segment names here are out of sync with 
        # the names used by write_parameters.
        #  this is relevant for salinity-file,  vert-diffusion-file
        #  maybe surfaces-file, depths-file.
        # for example, surfaces file is written as tbd-SURF.seg
        # but below we call it com-tbd.srf
        # maybe easiest to just change the code below since it's
        # already arbitrary
        fn=fn or os.path.join( self.scenario.base_path,
                               self.fn_base+".hyd")
        if os.path.exists(fn):
            self.log.warning("hyd file %s already exists.  Not overwriting!"%fn)
            return
        
        name=self.scenario.name

        dfmt="%Y%m%d%H%M%S"
        time_start = (self.time0+self.scen_t_secs[0]*self.scenario.scu)
        time_stop  = (self.time0+self.scen_t_secs[-1]*self.scenario.scu)
        timedelta = (self.t_secs[1] - self.t_secs[0])*self.scenario.scu
        timestep = timedelta_to_waq_timestep(timedelta)

        self.infer_2d_elements()
        n_layers=1+self.seg_k.max()

        # New code - maybe not right at all.
        if 'temp' in self.parameters():
            temp_file="'%s-temp.seg'"%name
        else:
            temp_file='none'
            
        lines=[
            "file-created-by  SFEI, waq_scenario.py",
            "file-creation-date  %s"%( datetime.datetime.utcnow().strftime('%H:%M:%S, %d-%m-%Y') ),
            "task      full-coupling",
            "geometry  unstructured",
            "horizontal-aggregation no",
            "reference-time           '%s'"%( self.time0.strftime(dfmt) ),
            "hydrodynamic-start-time  '%s'"%( time_start.strftime(dfmt) ),
            "hydrodynamic-stop-time   '%s'"%( time_stop.strftime(dfmt)  ),
            "hydrodynamic-timestep    '%s'"%timestep, 
            "conversion-ref-time      '%s'"%( self.time0.strftime(dfmt) ),
            "conversion-start-time    '%s'"%( time_start.strftime(dfmt) ),
            "conversion-stop-time     '%s'"%( time_stop.strftime(dfmt)  ),
            "conversion-timestep      '%s'"%timestep, 
            "grid-cells-first-direction       %d"%self.n_2d_elements,
            "grid-cells-second-direction          0",
            "number-hydrodynamic-layers          %s"%( n_layers ),
            "number-horizontal-exchanges      %d"%( self.n_exch_x ),
            "number-vertical-exchanges        %d"%( self.n_exch_z ),
            # little white lie.  this is the number in the top layer.
            # and no support for water-quality being different than hydrodynamic
            "number-water-quality-segments-per-layer       %d"%( self.n_2d_elements),
            "number-water-quality-layers          %s"%( n_layers ),
            "hydrodynamic-file        '%s'"%self.fn_base,
            "aggregation-file         none",
            # filename handling not as elegant as it could be..
            # e.g. self.vol_filename should probably be self.vol_filepath, then
            # here we could reference the filename relative to the hyd file
            "grid-indices-file     '%s.bnd'"%self.fn_base,# lies, damn lies
            "grid-coordinates-file '%s'"%self.flowgeom_filename,
            "attributes-file       '%s.atr'"%self.fn_base,
            "volumes-file          '%s.vol'"%self.fn_base,
            "areas-file            '%s.are'"%self.fn_base,
            "flows-file            '%s.flo'"%self.fn_base,
            "pointers-file         '%s.poi'"%self.fn_base,
            "lengths-file          '%s.len'"%self.fn_base,
            "salinity-file         '%s-salinity.seg'"%name,
            "temperature-file      %s"%temp_file,
            "vert-diffusion-file   '%s-vertdisper.seg'"%name,
            # not a segment function!
            "surfaces-file         '%s'"%self.surf_filename,
            "shear-stresses-file   none",
            "hydrodynamic-layers",
            "\n".join( ["%.5f"%(1./n_layers)] * n_layers ),
            "end-hydrodynamic-layers",
            "water-quality-layers   ",
            "\n".join( ["1.000"] * n_layers ),
            "end-water-quality-layers"]
        txt="\n".join(lines)
        with open(fn,'wt') as fp:
            fp.write(txt)

    @property
    def surf_filename(self):
        return self.fn_base+".srf"

    n_2d_links=None
    exch_to_2d_link=None
    links=None
    def infer_2d_links(self):
        """
        populate self.n_2d_links, self.exch_to_2d_link, self.links 
        note: compared to the incoming grid, this may include internal
        boundary exchanges.
        exchanges are identified based on unique from/to pairs of 2d elements.

        """
        if self.exch_to_2d_link is None:
            self.infer_2d_elements() 
            poi0=self.pointers-1

            #  map 0-based exchange index to 0-based link index
            exch_to_2d_link=np.zeros(self.n_exch_x+self.n_exch_y,[('link','i4'),
                                                                  ('sgn','i4')])
            exch_to_2d_link['link']=-1

            #  track some info about links
            links=[] # elt_from,elt_to
            mapped=dict() # (src_2d, dest_2d) => link idx

            # hmm - if there are multiple boundary exchanges coming into the
            # same segment, how can those be differentiated?  probably it's just
            # up to the sub-implementations to make the distinction.
            # so here they will get lumped together, but the datastructure should
            # allow for them to be distinct.

            for exch_i,(a,b,_,_) in enumerate(poi0[:self.n_exch_x+self.n_exch_y]):
                if a>=0:
                    a2d=self.seg_to_2d_element[a]
                else:
                    a2d=-1 # ??
                if b>=0:
                    b2d=self.seg_to_2d_element[b]
                else:
                    b2d=-1 # ??

                if (b2d,a2d) in mapped:
                    exch_to_2d_link['link'][exch_i] = mapped[(b2d,a2d)]
                    exch_to_2d_link['sgn'][exch_i]=-1
                else:
                    k=(a2d,b2d)
                    if k not in mapped:
                        mapped[k]=len(links)
                        links.append( [a2d,b2d] )
                    exch_to_2d_link['link'][exch_i] = mapped[k]
                    exch_to_2d_link['sgn'][exch_i]=1

            links=np.array(links)
            n_2d_links=len(links)

            # Bit of a sanity warning on multiple boundary exchanges involving the
            # same segment - this would indicate that there should be multiple 2D
            # links into that segment, but this generic code doesn't have a robust
            # way to deal with that.
            if 1:
                # indexes of which links are boundary
                bc_links=np.nonzero( links[:,0] < 0 )[0]

                for bc_link in bc_links:
                    # index of which exchanges map to this link
                    exchs=np.nonzero( exch_to_2d_link['link']==bc_link )[0]
                    # link id, sgn for each of those exchanges
                    ab=exch_to_2d_link[exchs]
                    # find the internal segments for each of those exchanges
                    segs=np.zeros(len(ab),'i4')
                    sel0=exch_to_2d_link['sgn'][exchs]>0 # regular order
                    segs[sel0]=poi0[exchs,1]
                    if np.any(~sel0):
                        # including checking for weirdness
                        self.log.warning("Some exchanges had to be flipped when flattening to 2D links")
                        segs[~sel0]=poi0[exchs,0]
                    # And finally, are there any duplicates into the same segment? i.e. a segment
                    # which has multiple boundary exchanges which we have failed to distinguish (since
                    # in this generic implementation we have little info for distinguishing them).
                    # note that in the case of suntans output, this is possible, but if it has been
                    # mapped from multiple domains to a global domain, those exchanges have probably
                    # already been combined.
                    if len(np.unique(segs)) < len(segs):
                        self.log.warning("In flattening exchanges to links, link %d has ambiguous multiple exchanges for the same segment"%bc_link)

            self.exch_to_2d_link=exch_to_2d_link
            self.links=links
            self.n_2d_links=n_2d_links
    def write_2d_links(self):
        """
        Write the results of infer_2d_links to two text files - directly mirror the
        structure of exch_to_2d_link and links.
        """
        self.infer_2d_links()
        path = self.scenario.base_path
        np.savetxt(os.path.join(path,'links.csv'),
                   self.links,fmt='%d')
        np.savetxt(os.path.join(path,'exch_to_2d_link.csv'),
                   self.exch_to_2d_link,fmt='%d')

    def path_to_transect_exchanges(self,xy,on_boundary='warn_and_skip'):
        """
        xy: [N,2] points.
        Each point is mapped to a node of the grid, and grid edges
        are identified to link up successive nodes.
        for each grid edge, find the exchanges which are part of that 2d link.
        return a list of these exchanges, but ONE BASED, where exchanges
        with their 'from' segment left of the path are positive, otherwise
        negated.

        on_boundary:
         'warn_and_skip': any of the edges which are closed edges in the original
            grid (unless a flow boundary), are mentioned, but omitted.
        """
        # align the input nodes along nodes of the grid
        g=self.grid()
        input_nodes=[g.select_nodes_nearest(p)
                     for p in xy]
        legs=[ input_nodes[0] ] 
        for a,b in zip(input_nodes[:-1],input_nodes[1:]):
            if a==b:
                continue
            path=g.shortest_path(a, b)
            legs+=list(path[1:])

        self.infer_2d_links()

        link_and_signs=[] # (link idx, sign to make from->to same as left->right
        for a,b in zip(legs[:-1],legs[1:]):
            j=g.nodes_to_edge(a,b)
            # possible to have missing cells with other marks (as in
            # marking an ocean or flow boundary), but boundary links are
            # just -1:
            c1_c2=g.edge_to_cells(j).clip(-1,g.Ncells())

            leg_to_edge_sign=1
            if g.edges['nodes'][j,0] == b:
                leg_to_edge_sign=-1

            # assumes that hydro elements and grid cells have the same numbering
            # make sure that any missing cell is just labeled -1
            fwd_hit= np.nonzero( np.all( self.links[:,:]==c1_c2, axis=1 ) )[0]
            rev_hit= np.nonzero( np.all( self.links[:,:]==c1_c2[::-1], axis=1 )) [0]
            nhits=len(fwd_hit)+len(rev_hit)
            if nhits==0:
                if np.any(c1_c2<0):
                    self.log.warning("Discarding boundary edge in path_to_transect_exchanges")
                    continue
                else:
                    raise Exception("Failed to match edge to link")
            elif nhits>1:
                raise Exception("Somehow got two matches.  Bad stuff.")

            if len(fwd_hit):
                link_and_sign = [fwd_hit[0],leg_to_edge_sign] 
            else:
                link_and_sign =[rev_hit[0],-leg_to_edge_sign]
            if link_and_signs and link_and_signs[-1][0]==link_and_sign[0]:
                self.log.warning("Discarding repeated link")
            else:
                link_and_signs.append(link_and_sign)

        link_to_exchs=defaultdict(list)
        for exch,(link,sgn) in enumerate(self.exch_to_2d_link):
            link_to_exchs[link].append( (exch,sgn) )

        transect_exchs=[]

        for link,sign in link_and_signs:
            for exch,exch_sign in link_to_exchs[link]:
                # here is where we switch to 1-based.
                transect_exchs.append( sign*exch_sign*(1+exch) )

        return transect_exchs
            
    link_group_dtype=[('id','i4'),
                      ('name','O'),
                      ('attrs','O')]
    def group_boundary_links(self):
        """ 
        a [hopeful] improvement over group_boundary_elements, since boundaries
        are properties of links.
        follows the same representation as group_boundary_elements, but enumerating
        2D links instead of 2D elements.
        
        maps all link ids (0-based) to either -1 (not a boundary)
        or a nonnegative id corresponding to contiguous boundary links which can
        be treated as a whole.

        This generic implementation isn't very smart, though.  We have so little
        geometry on boundary exchanges - so there's not a way to group boundary links
        which wouldn't risk grouping things which are really distinct.

        We could maybe conservatively do some grouping based on marked edges in the 
        original grid, but we don't necessarily have those marks in this code (but
        see Suntans subclass where more info is available).
        """
        self.infer_2d_links()

        bc_lgroups=np.zeros(self.n_2d_links,self.link_group_dtype)
        bc_lgroups['id']=-1 # most links are internal and not part of a boundary group
        for lg in bc_lgroups:
            lg['attrs']={} # we have no add'l information for the groups.
        sel_bc=np.nonzero( (self.links[:,0]<0) )[0]
        bc_lgroups['id'][sel_bc]=np.arange(len(sel_bc))
        bc_lgroups['name'][sel_bc]=['group %d'%i for i in bc_lgroups['id'][sel_bc]]
        return bc_lgroups

    def write_boundary_links(self):
        """ calls group_boundary_links, and writes the result out to a csv file,
        first few columns always the same: index (0-based, of the boundary link), 
        link0 (0-based index of the link, i.e. including all links), and a string-valued name.
        The rest of the fields are whatever group_boundary_links returned.  Some may
        have embedded commas and will be double-quote-escaped.  Results are written
        to boundary-links.csv in the base_path directory.
        """
        rows=[]
        gbl=self.group_boundary_links()
        
        for link_idx in range(len(gbl)):
            rec=gbl[link_idx]
            if rec['id']<0:
                continue
            row=OrderedDict()
            row['index']=rec['id']
            row['link0']=link_idx
            row['name']=rec['name']
            for k,v in iteritems(rec['attrs']):
                if k=='geom':
                    try:
                        v=v.wkt
                    except AttributeError:
                        # might not actually be a geometry object
                        pass
                if k not in row:
                    # careful not to allow incoming attributes to overwrite
                    # index or link0 from above
                    row[k]=v
            rows.append(row)

        df=pd.DataFrame(rows)
        # reorder those a bit..
        cols0=df.columns.tolist()
        cols=['index','link0','name']
        cols+=[c for c in cols0 if c not in cols]
        df=df[cols].set_index('index')
        if 0:
            # this is too strict. assumes both that these are sorted
            # and that every group has exactly one link.
            assert np.all( np.diff(df.index.values)==1 )
        if 1:
            # more lenient - just make sure that the id's present have 
            # no gaps
            unique_ids=np.unique(df.index.values) # sorted, too
            assert np.all(unique_ids == np.arange(len(unique_ids)))
        df.to_csv( os.path.join(self.scenario.base_path,"boundary-links.csv") )
    
    group_dtype=[('id','i4'), # 0-based id of this elements group, -1 for unset.
                 ('name','O'), # had been S40, but that get confusing with bytes vs. str
                 ('attrs','O')] # a place for add'l key-value pairs
    def group_boundary_elements(self):
        """ map all element ids (0-based) to either -1 (not a boundary)
        or a nonnegative id corresponding to contiguous boundary elements.
        
        Only works if a grid is available.
        """
        self.infer_2d_elements()

        g=self.grid()
        if g is None:
            # This code is wrong!
            # bc_groups should only be set for elements with a boundary link.
            assert False
            self.log.warning("No grid for grouping boundary elements")
            bc_groups=np.zeros(self.n_2d_elements,self.group_dtype)
            bc_groups['id']=np.arange(self.n_2d_elements)
            bc_groups['name']=['group %d'%i for i in self._bc_groups['id']]
            return bc_groups

        poi=self.pointers
        bc_sel = (poi[:,0]<0)
        bc_elts = np.unique(self.seg_to_2d_element[ poi[bc_sel,1]-1 ])

        def adjacent_cells(g,c,candidates):
            """ g: unstructured grid
            c: element/cell index
            candidates: subset of cells in the grid

            returns a list of cell ids which are adjacent to c and in candidates,
            based on two adjacency checks:
              shares an edge
              has boundary edges which share a node.
            """
            a=list(g.cell_to_adjacent_boundary_cells(c))
            b=list(g.cell_to_cells(c))
            nbrs=filter(lambda cc: cc in candidates,a+b)
            return np.unique(nbrs)

        groups=np.zeros(self.n_2d_elements,self.group_dtype)
        groups['id'] -= 1

        def trav(c,mark):
            groups['id'][c]=mark
            groups['name'][c]="group %d"%mark 
            for nbr in adjacent_cells(g,c,bc_elts):
                if groups['id'][nbr]<0:
                    trav(nbr,mark)

        ngroups=0
        for bc_elt in bc_elts:
            if groups['id'][bc_elt]<0:
                trav(bc_elt,ngroups)
                ngroups+=1
        return groups

def parse_datetime(s):
    """ 
    parse YYYYMMDDHHMMSS style dates.
    strips single quotes in case it came from a hyd file
    """
    return datetime.datetime.strptime(s.strip("'"),'%Y%m%d%H%M%S')        

class HydroFiles(Hydro):
    """ 
    dwaq hydro data read from existing files, by parsing
    .hyd file.
    """
    # if True, allow symlinking to original files where possible.
    enable_write_symlink=False

    def __init__(self,hyd_path):
        self.hyd_path=hyd_path
        self.parse_hyd()

        super(HydroFiles,self).__init__()

    def parse_hyd(self):
        self.hyd_toks={}

        with open(self.hyd_path,'rt') as fp:
            while 1:
                line=fp.readline().strip()
                if line=='':
                    break

                try:
                    tok,rest=line.split(None,1)
                except ValueError:
                    tok=line ; rest=None

                if tok in ['hydrodynamic-layers','water-quality-layers']:
                    layers=[]
                    while 1:
                        line=fp.readline().strip()
                        if line=='' or line=='end-'+tok:
                            break
                        layers.append(float(line))
                    self.hyd_toks[tok]=layers
                else:
                    self.hyd_toks[tok]=rest

    _t_secs=None
    @property
    def t_secs(self):
        if self._t_secs is None:
            conv_start=parse_datetime(self.hyd_toks['conversion-start-time'])
            conv_stop =parse_datetime(self.hyd_toks['conversion-stop-time'])
            conv_step = waq_timestep_to_timedelta(self.hyd_toks['conversion-timestep'].strip("'"))

            # important to keep all of these integers
            step_secs=conv_step.total_seconds() # seconds in a step
            n_steps=1+(conv_stop - conv_start).total_seconds() / step_secs
            n_steps=int(round(n_steps))
            start=(conv_start-self.time0).total_seconds()
            start=int(round(start))

            if abs(step_secs - round(step_secs)) > 1e-5:
                print("WARNING: total seconds in step was not an integer: %s"%step_secs)
            step_secs=int(round(step_secs))

            self._t_secs=(start+np.arange(n_steps)*step_secs).astype('i4')
        return self._t_secs

    @property
    def time0(self):
        return parse_datetime(self.hyd_toks['conversion-ref-time'])


    def __getitem__(self,k):
        val=self.hyd_toks[k]
        if k in ['grid-cells-first-direction',
                 'grid-cells-second-direction',
                 'number-hydrodynamic-layers',
                 'number-horizontal-exchanges',
                 'number-vertical-exchanges',
                 'number-water-quality-segments-per-layer',
                 'number-water-quality-layers']:
            return int(val)
        elif k in ['water-quality-layers',
                   'hydrodynamic-layers']:
            return val
        else:
            return val.strip("'")

    def get_dir(self):
        return os.path.dirname(self.hyd_path)
    
    def get_path(self,k,check=False):
        """ Return full pathname for a file referenced by its 
        key in .hyd.
        May throw KeyError.  

        check: if True, check that file exists, and throw KeyError otherwise
        """
        p=os.path.join( self.get_dir(),self[k] )
        if check and not os.path.exists(p):
            raise KeyError(p)
        return p

    _n_seg = None
    @property
    def n_seg(self):
        if self._n_seg is None:
            # assumes that every element has the same number of layers
            # in some processes dwaq assumes this, too!
            n_seg_dense=self['number-water-quality-layers'] * self['number-water-quality-segments-per-layer']

            # try to support partial water columns:

            nx=self['grid-cells-first-direction']
            ny=max(1,self['grid-cells-second-direction'])
            n_elts=nx*ny
            # bit of sanity check:
            if os.path.exists(self.get_path('grid-coordinates-file')):
                nc=qnc.QDataset( self.get_path('grid-coordinates-file') )
                n_elts_nc=len(nc.dimensions['nFlowElem'])
                nc.close()
                assert n_elts_nc==n_elts

            # assumes dense exchanges
            n_seg=self['number-vertical-exchanges'] + n_elts

            if 1: # allow for sparse exchanges and dense segments
                # more work, and requires that we have area and volume files
                nsteps=len(self.t_secs)

                # sanity check on areas:
                are_size=os.stat(self.get_path('areas-file')).st_size
                #pred_n_exch = (are_size/float(nsteps) - 4) / 4.
                #pred_n_exch2= (are_size/float(nsteps-1) - 4) / 4.
                #assert (pred_n_exch==self.n_exch) or (pred_n_exch2==self.n_exch)
                
                # kludge - suntans writer (as of 2016-07-13)
                # creates one fewer time-steps of exchange-related data
                # than volume-related data.
                # each step has 4 bytes per exchange, plus a 4 byte time stamp.
                pred_n_steps = are_size/4./(self.n_exch+1)
                if pred_n_steps==nsteps:
                    pass # great
                elif pred_n_steps==nsteps-1:
                    self.log.info("Area file has one fewer time step than expected - probably SUNTANS trancription")
                elif pred_n_steps==nsteps+1:
                    self.log.info("Area file has one more time step than expected - probably okay?")
                else:
                    raise Exception("nsteps %s too different from size of area file (~ %s steps)"%(nsteps,
                                                                                                   pred_n_steps))
                vol_size=os.stat(self.get_path('volumes-file')).st_size
                # kludgY.  Ideally have the same number of volume and area output timesteps, but commonly
                # one off.
                for vol_n_steps in pred_n_steps,pred_n_steps+1:
                    n_seg = (vol_size/float(vol_n_steps) -4)/ 4.
                    if n_seg%1.0 != 0.0:
                        continue
                    else:
                        n_seg=int(n_seg)
                        break
                else:
                    raise Exception("Volume file has neither %d nor %d steps"%(pred_n_steps,pred_n_steps+1))

                if n_seg==n_seg_dense:
                    self.log.debug("Discovered that hydro is dense")

            # make sure we're consistent with pointers --
            assert n_seg>=self.pointers.max()
            self._n_seg=n_seg
        return self._n_seg

    @property
    def n_exch_x(self):
        return self['number-horizontal-exchanges']
    @property
    def n_exch_y(self):
        return 0
    @property
    def n_exch_z(self):
        return self['number-vertical-exchanges']

    @property
    def exchange_lengths(self):
        with open(self.get_path('lengths-file'),'rb') as fp:
            n_exch=np.fromfile(fp,'i4',1)[0]
            assert n_exch == self.n_exch
            return np.fromfile(fp,'f4',2*self.n_exch).reshape( (self.n_exch,2) )

    def write_are(self):
        if not self.enable_write_symlink:
            return super(HydroFiles,self).write_are()
        else:
            rel_symlink(self.get_path('areas-file'),
                        self.are_filename)

    def areas(self,t):
        ti=self.time_to_index(t)

        stride=4+self.n_exch*4
        area_fn=self.get_path('areas-file')
        with open(area_fn,'rb') as fp:
            fp.seek(stride*ti)
            tstamp_data=fp.read(4)
            if len(tstamp_data)<4 and ti==len(self.t_secs)-1:
                self.log.info("Short read on last frame of area data - use prev")
                assert ti>0
                fp.seek(stride*(ti-1))
                tstamp_data=fp.read(4)
            else:
                tstamp=np.fromstring(tstamp_data,'i4')[0]
                if tstamp!=t:
                    print("WARNING: time stamp mismatch: %d [file] != %d [requested]"%(tstamp,t))
            return np.fromstring(fp.read(self.n_exch*4),'f4')

    def write_vol(self):
        if not self.enable_write_symlink:
            return super(HydroFiles,self).write_vol()
        else:
            rel_symlink(self.get_path('volumes-file'),
                        self.vol_filename)

    def volumes(self,t):
        return self.seg_func(t,label='volumes-file')

    def seg_func(self,t_sec=None,fn=None,label=None):
        """ 
        Get segment function data at a given timestamp (must match a timestamp
        - no interpolation).
        t: time in seconds, or a datetime instance
        fn: full path to data file
        label: key in the hydr file (e.g. "volumes-file")
        
        if t_sec is not specified, returns a callable which takes t_sec
        """
        def f(t_sec,closest=False):
            if isinstance(t_sec,datetime.datetime):
                t_sec = int( (t_sec - self.time0).total_seconds() )
            
            filename=fn or self.get_path(label)
            # Optimistically assume that the seg function has the same time steps
            # as the hydro:
            ti=self.time_to_index(t_sec) 

            stride=4+self.n_seg*4
            
            with open(filename,'rb') as fp:
                fp.seek(stride*ti)
                tstamp=np.fromfile(fp,'i4',1)
                
                if len(tstamp)==0 or tstamp[0]!=t_sec:
                    if 0:# old behavior, no scanning:
                        if len(tstamp)==0:
                            print("WARNING: no timestamp read for seg function")
                        else: 
                            print("WARNING: time stamp mismatch: %s != %d should scan but won't"%(tstamp[0],t_sec))
                    else: # new behavior to accomodate hydro parameters with variable time steps
                        # assumes at least two time steps, and that all steps are the same size
                        fp.seek(0)
                        tstamp0=np.fromfile(fp,'i4',1)[0]
                        fp.seek(stride*1)
                        tstamp1=np.fromfile(fp,'i4',1)[0]
                        dt=tstamp1 - tstamp0
                        ti,err=divmod( t_sec-tstamp0, dt )
                        if err!=0:
                            print("WARNING: time stamp mismatch after inferring nonstandard time step")
                        # also check for bounds:
                        warning=None
                        if ti<0:
                            if t_sec>=0:
                                warning="WARNING: inferred time index %d is negative!"%ti
                            else:
                                # kludgey - the problem is that something like the temperature field
                                # can have a different time line, and to be sure that it has data
                                # t=0, an extra step at t<0 is included.  But then there isn't any
                                # volume data to be used, and that comes through here, too.
                                # so downgrade it to a less dire message
                                warning="INFO: inferred time index %d is negative, ignoring as t=%d"%(ti,t_sec)
                            ti=0
                        max_ti=os.stat(filename).st_size // stride
                        if ti>=max_ti:
                            warning="WARNING: inferred time index %d is beyond the end of the file!"%ti
                            ti=max_ti-1
                        # try that again:
                        fp.seek(stride*ti)
                        tstamp=np.fromfile(fp,'i4',1)
                        if warning is None and tstamp[0]!=t_sec:
                            warning="WARNING: Segment function appears to have unequal steps"
                        if warning:
                            #import pdb
                            #pdb.set_trace()
                            print(warning)

                return np.fromfile(fp,'f4',self.n_seg)
        if t_sec is None:
            return f
        else:
            return f(t_sec)

    def vert_diffs(self,t_sec):
        return self.seg_func(t_sec,label='vert-diffusion-file')

    def write_flo(self):
        if not self.enable_write_symlink:
            return super(HydroFiles,self).write_flo()
        else:
            rel_symlink(self.get_path('flows-file'),
                        self.flo_filename)


    def flows(self,t):
        """ returns flow rates ~ np.zeros(self.n_exch,'f4'), for given timestep.
        flows in m3/s.  Sometimes there is no flow data for the last timestep,
        since flow is integrated over [t,t+dt].  Checks file size and may return
        zero flow
        """
        ti=self.time_to_index(t)
        
        stride=4+self.n_exch*4
        flo_fn=self.get_path('flows-file')
        with open(flo_fn,'rb') as fp:
            fp.seek(stride*ti)
            tstamp_data=fp.read(4)
            if len(tstamp_data)<4 and ti==len(self.t_secs)-1:
                self.log.info("Short read on last frame of flow data - fabricate zero flows")
                return np.zeros(self.n_exch,'f4')
            else:
                tstamp=np.fromstring(tstamp_data,'i4')[0]
                if tstamp!=t:
                    print("WARNING: time stamp mismatch: %d != %d"%(tstamp,t))
                return np.fromstring(fp.read(self.n_exch*4),'f4')

    @property
    def pointers(self):
        poi_fn=self.get_path('pointers-file')
        with open(poi_fn,'rb') as fp:
            return np.fromstring( fp.read(), 'i4').reshape( (self.n_exch,4) )

    def planform_areas(self):
        # any chance we have this info written out to file?
        # seems like there are two competing ideas of what is in surfaces-file
        # DwaqAggregator might have written this out as if it were a segment
        # function
        # but here it's expected to be constant in time, and have some header info
        # okay - see delwaq.c or waqfil.m or details on the format.
        if 'surfaces-file' in self.hyd_toks:
            # actually would be pretty easy, but not implemented yet.
            srf_fn=self.get_path('surfaces-file')
            with open(srf_fn,'rb') as fp:
                hdr=np.fromfile(fp,np.int32,6)
                # following waqfil.m
                elt_areas=np.fromfile(fp,np.float32,hdr[2])
            self.infer_2d_elements()
            assert self.n_2d_elements==len(elt_areas)
            return ParameterSpatial(elt_areas[self.seg_to_2d_element],hydro=self)
        else:
            # cobble together areas from the exchange areas
            seg_z_exch=self.seg_to_exch_z(preference='upper')

            # then pull exchange area for each time step
            A=np.zeros( (len(self.t_secs),self.n_seg) )
            # some segments have no vertical exchanges - they'll just get
            # A=1 (below) for lack of a better guess.
            sel=seg_z_exch>=0
            for ti,t_sec in enumerate(self.t_secs):
                areas=self.areas(t_sec)
                A[ti,sel] = areas[seg_z_exch[sel]]

            # without this, but with zero area exchanges, and monotonicize
            # enabled, it was crashing, complaining that DDEPTH ran into
            # zero SURF.
            # enabling this lets it run, though depths are pretty wacky.
            A[ A<1.0 ] = 1.0
            return ParameterSpatioTemporal(times=self.t_secs,values=A,hydro=self)

    # segment attributes - namely surface/middle/bed
    _read_seg_attrs=None # seg attributes read from file. shaped [nseg,2]
    def seg_attrs(self,number):
        """ corresponds to the 'number 2' set of properties, unless number is specified.
        number is 1-based!
        """
        if self._read_seg_attrs is None and 'attributes-file' in self.hyd_toks:
            self.log.debug("Reading segment attributes from file")
    
            seg_attrs=np.zeros( (self.n_seg,2), 'i4')
            seg_attrs[:,0] = 1 # default for active segments
            seg_attrs[:,1] = 0 # default, depth-averaged segments
            
            with open(self.get_path('attributes-file'),'rt') as fp:
                # this should all be integers
                toker=lambda t=tokenize(fp): int(next(t))
                
                n_const_blocks=toker()
                for const_contrib in range(n_const_blocks):
                    nitems=toker()
                    feat_numbers=[toker() for item in range(nitems)]
                    assert nitems==1 # not implemented for multiple..
                    assert toker()==1 # input is in this file, nothing else implemented.
                    assert toker()==1 # all segments written, no defaults.
                    for seg in range(self.n_seg):
                        seg_attrs[seg,feat_numbers[0]-1] = toker()
                n_variable_blocks=toker()
                assert n_variable_blocks==0 # not implemented
            self._read_seg_attrs=seg_attrs
        if self._read_seg_attrs is not None:
            assert number>0
            return self._read_seg_attrs[:,number-1]
        else:
            return super(HydroFiles,self).seg_attrs(number)

    def write_geom(self):
        # just copy existing grid geometry
        try:
            orig=self.get_path('grid-coordinates-file',check=True)
        except KeyError:
            return

        dest=os.path.join(self.scenario.base_path,
                          self.flowgeom_filename)
        if self.enable_write_symlink:
            rel_symlink(orig,dest)
        else:
            shutil.copyfile(orig,dest)
    def get_geom(self):
        try:
            return xr.open_dataset( self.get_path('grid-coordinates-file',check=True) )
        except KeyError:
            return
        

    _grid=None
    def grid(self,force=False):
        if force or self._grid is None:
            try:
                orig=self.get_path('grid-coordinates-file',check=True)
            except KeyError:
                return None
            ug=ugrid.Ugrid(orig)
            self._grid=ug.grid()
        return self._grid

    def add_parameters(self,hyd):
        super(HydroFiles,self).add_parameters(hyd)

        # can probably add bottomdept,depth,salinity.

        # do NOT include surfaces-files here - it's a different format.
        for var,key in [('vertdisper','vert-diffusion-file'),
                        #('tau','shear-stresses-file'),
                        ('temp','temperature-file'),
                        ('salinity','salinity-file')]:
            fn=self.get_path(key)
            if os.path.exists(fn):
                hyd[var]=ParameterSpatioTemporal(seg_func_file=fn,enable_write_symlink=True,
                                                 hydro=self)
        return hyd

    def read_2d_links(self):
        """
        Read the files written by self.write_2d_links, set attributes, and return true.
        If the files don't exist, return False, log an info message.
        """
        path = self.get_dir()
        try:
            links=np.loadtxt(os.path.join(path,'links.csv'),dtype='i4')
            exch_to_2d_link = np.loadtxt(os.path.join(path,'exch_to_2d_link.csv'),
                                         dtype=[('link','i4'),('sgn','i4')])
        except FileNotFoundError:
            return False
        self.links=links
        self.exch_to_2d_link=exch_to_2d_link
        self.n_2d_links=len(links)
        return True

    def infer_2d_links(self,force=False):
        if self.exch_to_2d_link is not None and not force:
            return

        if not self.read_2d_links():
            self.log.warning("Couldn't read 2D link info in HydroFiles - will compute")
            super(HydroFiles,self).infer_2d_links()

    def group_boundary_links(self):
        gbl=self.read_group_boundary_links()
        if gbl is None:
            self.log.info("Couldn't find file with group_boundary_links data")
            return super(HydroFiles,self).group_boundary_links()
        else:
            return gbl
        
    def read_group_boundary_links(self):
        """ Attempt to read grouped boundary links from file.  Return None if
        file doesn't exist.
        """
        gbl_fn=os.path.join(self.get_dir(),'boundary-links.csv')
        if not os.path.exists(gbl_fn):
            return None
        
        df=pd.read_csv(gbl_fn)

        self.infer_2d_links()
        gbl=np.zeros( self.n_2d_links,self.link_group_dtype )
        gbl['id']=-1 # initialize to non-boundary
        for reci,rec in df.iterrows():
            link0=rec['link0']
            gbl['id'][link0]=reci # not necessarily unique!
            gbl['name'][link0]=rec['name']
            other={}
            for k,v in rec.iteritems():
                # name is repeated here to make downstream code simpler
                if k not in ['id']:
                    # could be cleverish and convert 'geom' WKT back to geometry.
                    # asking a little much, I'd say.
                    other[k]=v
            gbl['attrs'][link0]=other

        return gbl
        

REINDEX=-9999
class DwaqAggregator(Hydro):
    REINDEX=REINDEX

    # whether or not to force all layers to have the same number of
    # segments.
    sparse_layers=True

    # if True, boundary exchanges are combined so that any given segment has
    # at most one boundary exchange
    # if False, these exchanges are kept distinct, leading to easier addressing
    # of boundary conditions but pressing one's luck in terms of how many exchanges
    # can go to a single segment.
    agg_boundaries=True

    # how many of these can be factor out into above classes?
    # agg_shp: can specify, but if not specified, will try to generate a 1:1 
    #   mapping
    # run_prefix: 
    # path:
    # nprocs: 
    def __init__(self,agg_shp=None,nprocs=None,skip_load_basic=False,sparse_layers=None,
                 merge_only=False,
                 **kwargs):
        super(DwaqAggregator,self).__init__(**kwargs)
        # where/how to auto-create agg_shp??
        # where is it first needed? load_basic -> init_elt_mapping -> init_agg_elements_2d
        # what is the desired behavior?
        #   what about several possible types for agg_shp?
        #    - filename -> load as shapefile, extract name if it exists
        #    - an unstructured grid -> load those cells, use cell id for name.
        if sparse_layers is not None:
            self.sparse_layers=sparse_layers

        # some steps can be streamline when we know that there is no aggregation, just
        # merging multiprocessor to single domain, and no change of ordering for elements
        self.merge_only=merge_only

        self.agg_shp=agg_shp
        if nprocs is None:
            nprocs=self.infer_nprocs()
        self.nprocs=nprocs

        if not skip_load_basic:
            self.load_basic()

    def load_basic(self):
        """ 
        populate general, time-invariant info
        """
        self.find_maxima()

        self.init_elt_mapping()
        self.init_seg_mapping()
        self.init_exch_mapping()
        self.reindex()
        self.add_exchange_data()
                    
        self.init_exch_matrices()
        self.init_seg_matrices()
        self.init_boundary_matrices()

    def open_hyd(self,p,force=False):
        raise Exception("Must be overloaded")

    _flowgeoms=None
    def open_flowgeom(self,p):
        # hopefully this works for both subclasses...
        if self._flowgeoms is None:
            self._flowgeoms={}
        if p not in self._flowgeoms:
            hyd=self.open_hyd(p)
            fg=qnc.QDataset(hyd.get_path('grid-coordinates-file'))
            self._flowgeoms[p] = fg
        return self._flowgeoms[p]

    agg_elt_2d_dtype=[('plan_area','f4'),('name','S100'),('zcc','f4'),('poly',object)]
    agg_seg_dtype=[('k','i4'),('elt','i4'),('active','b1')]
    agg_exch_dtype=[('from_2d','i4'),('from','i4'),
                    ('to_2d','i4'),('to','i4'),
                    ('from_len','f4'),('to_len','f4'),
                    ('direc','S1'),
                    ('k','f4') # float: vertical exchanges have fractional layer.
    ]

    @property
    def n_seg(self):
        return self.n_agg_segments

    def seg_active(self):
        # return boolean array of whether each segment is active
        return self.agg_seg['active']

    def seg_attrs(self,number):
        if number==1:
            return self.seg_active().astype('i4')
        elif number==2:
            return super(DwaqAggregator,self).seg_attrs(number=number)

    def init_agg_elements_2d(self):
        """
        load the aggregation polygons, setup the corresponding 2D 
        data for those elements.

        populates self.elements, self.n_agg_elements_2d
        """
        if isinstance(self.agg_shp,str):
            box_defs=wkb2shp.shp2geom(self.agg_shp)
            box_polys=box_defs['geom']
            try:
                box_names=box_defs['name']
            except ValueError:
                box_names=["%d"%i for i in range(len(box_polys))]
        else:
            agg_shp=self.agg_shp
            if agg_shp is None and self.nprocs==1:
                agg_shp=self.open_hyd(0).grid()

            if isinstance(agg_shp,unstructured_grid.UnstructuredGrid):
                g=agg_shp
                box_polys=[g.cell_polygon(i) for i in g.valid_cell_iter()]
                box_names=["%d"%i for i in range(len(box_polys))]
            else:
                raise Exception("Need some guidance on agg_shp")

        self.n_agg_elements_2d=len(box_polys)

        agg_elts_2d=[]
        for agg_i in range(self.n_agg_elements_2d):
            elem=np.zeros((),dtype=self.agg_elt_2d_dtype)
            elem['name']=box_names[agg_i]
            elem['poly']=box_polys[agg_i]
            agg_elts_2d.append(elem)
        # per http://stackoverflow.com/questions/15673155/keep-getting-valueerror-with-numpy-while-trying-to-create-array
        self.elements=rfn.stack_arrays(agg_elts_2d)

    def find_maxima(self):
        """
        find max global flow element id to preallocate mapping table
        and the links
        """
        max_gid=0
        max_elts_2d_per_proc=0
        max_lnks_2d_per_proc=0

        for p in range(self.nprocs):
            nc=self.open_flowgeom(p)
            max_gid=max(max_gid, nc.FlowElemGlobalNr[:].max() )
            max_elts_2d_per_proc=max(max_elts_2d_per_proc,len(nc.dimensions['nFlowElem']))
            max_lnks_2d_per_proc=max(max_lnks_2d_per_proc,len(nc.dimensions['nFlowLink']))
            # nc.close() # now we cache this

        n_global_elements=max_gid # ids should be 1-based, so this is also the count

        # For exchanges, have to be get a bit more involved - in fact, just consult
        # the hyd file, since we don't know whether closed exchanges are included or not.
        max_hor_exch_per_proc=0
        max_ver_exch_per_proc=0
        max_exch_per_proc=0
        max_bc_exch_per_proc=0
        for p in range(self.nprocs):
            hyd=self.open_hyd(p)
            n_hor=hyd['number-horizontal-exchanges']
            n_ver=hyd['number-vertical-exchanges']
            max_hor_exch_per_proc=max(max_hor_exch_per_proc,n_hor)
            max_ver_exch_per_proc=max(max_ver_exch_per_proc,n_ver)
            max_exch_per_proc=max(max_exch_per_proc,n_hor+n_ver)
            
            poi=hyd.pointers
            n_bc=np.sum( poi[:,:2] < 0 )
            max_bc_exch_per_proc=max(max_bc_exch_per_proc,n_bc)

            if p==0:
                # more generally the *max* number of layers
                self.n_src_layers=hyd['number-water-quality-layers']

        # could be overridden, but take max number of aggregated layers to be
        # same as max number of unaggregated layers
        self.n_agg_layers=self.n_src_layers

        max_segs_per_proc=self.n_src_layers * max_elts_2d_per_proc

        self.log.debug("Max global flow element id: %d"%max_gid )
        self.log.debug("Max 2D elements per processor: %d"%max_elts_2d_per_proc )
        self.log.debug("Max 3D segments per processor: %d"%max_segs_per_proc)
        self.log.debug("Max 3D exchanges per processor: %d (h: %d,v: %d)"%(max_exch_per_proc,
                                                                  max_hor_exch_per_proc,
                                                                  max_ver_exch_per_proc))
        self.log.debug("Max 3D boundary exchanges per processor: %d"%(max_bc_exch_per_proc))

        self.n_global_elements=n_global_elements
        self.max_segs_per_proc=max_segs_per_proc
        self.max_gid=max_gid
        self.max_exch_per_proc=max_exch_per_proc
        self.max_hor_exch_per_proc=max_hor_exch_per_proc
        self.max_ver_exch_per_proc=max_ver_exch_per_proc
        self.max_bc_exch_per_proc=max_bc_exch_per_proc

    
    # fast-path for matching local elements to aggregation polys.
    agg_query_size=5 
    def init_elt_mapping(self):
        """
        Map global, 2d element ids to 2d boxes (i.e. flowgeom)
        """
        self.init_agg_elements_2d()

        # initialize to -1, signifying that unaggregated elts are by default
        # not mapped to an aggregated element.
        self.elt_global_to_agg_2d=np.zeros(self.n_global_elements,'i4') - 1

        self.elements['plan_area']=0.0
        self.elements['zcc']=0.0

        # best way to speed this up?
        # right now, 3 loops
        # processors
        #  cells on processor
        #    polys in agg_shp.

        # a kd-tree of the agg_shp centroids?
        # this is still really bad in the case where there are many local cells,
        # many aggregation polys, but the latter do not cover the former. 
        # For that, RTree would be helpful since it can handle real overlaps.
        if not self.merge_only:
            agg_centers=np.array( [p.centroid.coords[0] for p in self.elements['poly']] )
            kdt=scipy.spatial.KDTree(agg_centers)
            total_poly=[None] # box it for py2/py3 compatibility
        else:
            kdt=total_poly=agg_centers="na"

        def match_center_to_agg_poly(x,y):
            pnt=geometry.Point(x,y)
            
            dists,idxs=kdt.query([x,y],self.agg_query_size)
            
            for poly_i in idxs:
                if self.elements['poly'][poly_i].contains(pnt):
                    return poly_i
            else:
                self.log.debug("Falling back on exhaustive search")

            if total_poly[0] is None and cascaded_union is not None:
                self.log.info("Calculating union of all aggregation polys")
                total_poly[0] = cascaded_union(self.elements['poly'])
            if total_poly[0] is not None and not total_poly[0].contains(pnt):
                return None

            for poly_i,poly in enumerate(self.elements['poly']):
                if poly.contains(pnt):
                    return poly_i
            else:
                return None

        for p in range(self.nprocs):
            self.log.info("init_elt_mapping: proc=%d"%p)
            nc=self.open_flowgeom(p)
            ccx=nc.FlowElem_xcc[:]
            ccy=nc.FlowElem_ycc[:]
            ccz=nc.FlowElem_zcc[:]

            dom_id=nc.FlowElemDomain[:]
            global_ids=nc.FlowElemGlobalNr[:] - 1  # make 0-based
            areas=nc.FlowElem_bac[:]

            vols=areas*ccz # volume to nominal MSL

            n_local_elt=len(ccx)
            hits=0
            # as written, quadratic in the number of cells.
            for local_i in range(n_local_elt):
                if dom_id[local_i]!=p:
                    continue # only check elements in their native subdomain
                if self.merge_only:
                    poly_i=global_ids[local_i]
                else:
                    poly_i = match_center_to_agg_poly(ccx[local_i],
                                                      ccy[local_i] )
                    if poly_i is None:
                        continue

                if hits%2000==0:
                    self.log.info('2D element within aggregation polygon: %d'%hits)
                hits+=1

                self.elt_global_to_agg_2d[global_ids[local_i]]=poly_i
                self.elements[poly_i]['plan_area'] = self.elements[poly_i]['plan_area'] + areas[local_i]
                # weighted by area, to be normalized below.
                self.elements[poly_i]['zcc'] = self.elements[poly_i]['zcc'] + vols[local_i]
                                
            msg="Processor %4d: %6d 2D elements within an aggregation poly"%(p,hits)
            if hits:
                self.log.info(msg)
            else:
                self.log.debug(msg)

        # and normalize those depths
        self.elements['zcc'] = self.elements['zcc'] / self.elements['plan_area']

        print("-"*40)
        n_elt_to_print=10
        if len(self.elements)>n_elt_to_print:
            print("Only showing first %d elements"%n_elt_to_print)

        for elt2d in self.elements[:n_elt_to_print]:
            self.log.info("{:<20}   area: {:6.2f} km2  mean"
                          " depth to 0 datum: {:5.2f} m".format(elt2d['name'],
                                                                elt2d['plan_area']/1e6,
                                                                elt2d['zcc']))

    def init_seg_mapping(self):
        """
        And use that to also build up map of (proc,local_seg_index) => agg_segment for 3D
        populates 
        self.seg_local_to_agg[nprocs,max_seg_per_proc]
         - maps processor-local segment indexes to aggregated segment, all 0-based

        segments are added to agg_seg, but not necessarily in final order.
        """
        # maybe hold off, get a real number?
        # self.n_agg_segments=self.agg_linear_map.max()+1

        self.agg_seg=[]
        self.agg_seg_hash={} # k,elt => index into agg_seg
        
        self.seg_local_to_agg=np.zeros( (self.nprocs,self.max_segs_per_proc),'i4')
        self.seg_local_to_agg[...] = -1

        if not self.sparse_layers:
            # pre-allocate segments to set them to inactive -
            # then below the code make them active as it goes.
            # inactive_segs then is set based on agg_seg[]['active']
            
            # set all agg_segs to inactive, and make them active only when somebody
            # maps to them.
            for agg_elt in range(self.n_agg_elements_2d):
                for k in range(self.n_agg_layers):
                    seg=self.get_agg_segment(agg_k=k,agg_elt=agg_elt)
                    self.agg_seg[seg]['active']=False
                    
        for p in range(self.nprocs):
            nc=self.open_flowgeom(p) 
            seg_to_2d=None # lazy loaded

            dom_id=nc.FlowElemDomain[:]
            n_local_elt=len(dom_id)
            global_ids=nc.FlowElemGlobalNr[:]-1 # make 0-based
            # nc.close() # now we cache this

            for local_i in range(n_local_elt):
                global_id=global_ids[local_i]
                agg_elt=self.elt_global_to_agg_2d[global_id]
                if agg_elt<0:
                    continue # not in an aggreg. polygon

                # A bit confusing, but this is *not* the right place to 
                # test for dom_id==p - these ghost segments are needed for
                # the exchange mapping.
                # So probably the wrong place to include this
                # if dom_id!=p: 
                #     continue 

                # and 3D mapping:
                if seg_to_2d is None:
                    hyd=self.open_hyd(p)
                    hyd.infer_2d_elements()
                    seg_to_2d=hyd.seg_to_2d_element

                # this is going to be slow...
                segs=np.nonzero(seg_to_2d==local_i)[0]
                
                for k,local_3d in enumerate(segs):
                    # assumption: the local segments
                    # start with the surface, and match with the top subset
                    # of aggregated segments (i.e. hydro layers same as
                    # aggregated layers, except hydro may be missing deep
                    # layers).  that is what lets us use k (being the index
                    # of local segments in this local elt) as an equivalent
                    # to k_agg
                    one_agg_seg=self.get_agg_segment(agg_k=k,agg_elt=agg_elt)
                    self.seg_local_to_agg[p,local_3d]=one_agg_seg
                    self.agg_seg[one_agg_seg]['active']=True

                # OBSOLETE: split to before/after this loop. This here causes
                # problems with dense/aggregated output.
                # if not self.sparse_layers:
                #     for k in range(len(segs),self.n_agg_layers):
                #         seg=self.get_agg_segment(agg_k=k,agg_elt=agg_elt) 
                #         self.agg_seg[seg]['active']=False
                #         self.inactive_segs.append(seg)

        # make a separate list of inactive segments
        self.inactive_segs=[]
        for agg_segi,agg_seg in enumerate(self.agg_seg):
            if not agg_seg['active']:
                self.inactive_segs.append(agg_segi)

                        
    def init_exch_mapping(self):
        """
        populate self.exch_local_to_agg[ nproc,max_exch_per_proc ]
         maps to aggregated exchange indexes.
         (-1: not mapped, otherwise 0-based index of exchange)
        and exch_local_to_agg_sgn, same size, but -1,1 depending on
         how the sign should map, or 0 if the exchange is not mapped.

        bc_local_to_agg used to be here, but became crufty.
        """
        # boundaries:
        # how are boundaries dealt with in existing poi?
        # see check_boundary_assumptions() for some details and verification

        # boundaries are always listed with the outside (negative)
        # index first (the from segment).
        # each segment with a boundary face gets its own boundary face index.
        # decreasing negative indices in poi correspond to the increasing positive
        # indices in flowgeom (-1=>nelem+1, -2=>nelem+2,...)
        
        # so far, all boundary exchanges are completely local - the internal segment
        # is always local, and the exchange is always local.  this code makes
        # a less stringent assumption - that the exchange is local iff the internal
        # segment is local.

        self.agg_exch=[] # entries add via reg_agg_exch=>get_agg_exch
        self.agg_exch_hash={} # (agg_from,agg_to) => index into agg_exch

        self.exch_local_to_agg=np.zeros( (self.nprocs,self.max_exch_per_proc),'i4') 
        self.exch_local_to_agg[...] = -1
        # sign is separate - should be 1 or -1 (or 0 if no mapping)
        self.exch_local_to_agg_sgn=np.zeros( (self.nprocs,self.max_exch_per_proc),'i4') 

        n_layers=self.n_agg_layers

        for p in range(self.nprocs):
            # this loop is a little slow.
            # skip a processor if it has no segments within
            # our aggregation regions:
            if np.all(self.seg_local_to_agg[p,:]<0):
                self.log.debug("Processor %d - skipped"%p)
                continue

            self.log.debug("Processor %d"%p)

            hyd=self.open_hyd(p)
            pointers=hyd.pointers

            nc=self.open_flowgeom(p) # for subdomain ownership

            elem_dom_id=nc.FlowElemDomain[:] # domains are numbered from 0
            link_dom_id=nc.FlowLinkDomain[:] # 

            n_hor=hyd['number-horizontal-exchanges']
            n_ver=hyd['number-vertical-exchanges']

            nFlowElem=len(nc.dimensions['nFlowElem'])

            pointers2d=pointers[:,:2].copy()
            sel=(pointers2d>0) # only map non-boundary segments
            hyd.infer_2d_elements() # all 0-based..
            pointers2d[sel] = hyd.seg_to_2d_element[ pointers2d[sel]-1 ] + 1

            # But really this code is problematic when dealing with z-level
            # or arbitrary grids.  Is it possible to map to 2D elements based
            # on order, but only for the top layer?
            # should be...

            links=nc.FlowLink[:] # 1-based
            def local_elts_to_link(from_2d,to_2d):
                # slow - have to lookup the link
                sela=(links[:,0]==from_2d)&(links[:,1]==to_2d)
                selb=(links[:,0]==to_2d)  &(links[:,1]==from_2d)
                idxs=np.nonzero(sela|selb)[0]
                assert(len(idxs)==1)
                return idxs[0] # 0-based index

            hits=0
            for local_i in range(len(pointers)):
                # these are *all* 1-based indices
                local_from,local_to=pointers[local_i,:2]
                from_2d,to_2d=pointers2d[local_i,:2]

                if local_i<n_hor:
                    direc='x' # structured grids not supported, no 'y'
                else:
                    direc='z'

                # Is the exchange real and local
                #
                if local_from==self.CLOSED or local_to==self.CLOSED:
                    continue # just omit CLOSED exchanges.
                elif local_to<0:
                    raise Exception("Failed assumption that boundary exchange is always first")
                elif local_from<0:
                    # it's a true boundary in the original grid
                    assert direc is 'x' # really hope that it's horizontal

                    # is it a ghost?  Check based on locality of the internal segment:
                    internal_is_local=(elem_dom_id[to_2d-1]==p)

                    # should match locality of the link
                    # this makes too many assumptions about constant number of 
                    # exchanges per layer - in regular grids this was never violated

                    # assert internal_is_local==(link_dom_id[link_2d]==p)
                    if not internal_is_local:
                        continue
                    # it's a true boundary, and not a ghost - proceed
                else:
                    # it's a real exchange, but might be a ghost.
                    if direc is 'x': # horizontal - could be a ghost
                        assert from_2d!=to_2d
                        # quick check - if both elements are local, then the
                        # link is local (true for the files I have)
                        # convert to 0-based for indexing
                        if elem_dom_id[from_2d-1]==p and elem_dom_id[to_2d-1]==p:
                            # it's local
                            pass
                        else:
                            link_idx=local_elts_to_link(from_2d,to_2d)
                                
                            if link_dom_id[link_idx]!=p:
                                # print("ghost horizontal - skip")
                                continue
                            else:
                                # looked up the link, and it is local.
                                # for sanity - local links have at least one local
                                # element
                                if elem_dom_id[from_2d-1]!=p and elem_dom_id[to_2d-1]!=p:
                                    print( "[proc=%d] from_2d=%d  to_2d=%d"%(p,from_2d,to_2d) )
                                    print( "          from_3d=%d  to_3d=%d"%(local_from,local_to) )
                                    print( "              dom=%d    dom=%d"%(elem_dom_id[from_2d],
                                                                            elem_dom_id[to_2d]) )
                                    assert False
                    elif direc is 'z':
                        # it's vertical - check if the elt is a ghost
                        if elem_dom_id[from_2d-1]!=p:
                            #print "ghost vertical - skip"
                            continue

                # so it's real (not closed), and local (not ghost)
                # might be an unaggregated boundary, in which case local_from<0

                if local_from<0:
                    # boundary
                    agg_to=self.seg_local_to_agg[p,local_to-1]
                    agg_from=BOUNDARY
                    if agg_to==-1:
                        # unaggregated boundary exchange going to segment we aren't
                        # tracking
                        continue
                    else:
                        # a real unaggregated boundary which we have to track.
                        self.reg_agg_exch(direc=direc,
                                          agg_to=agg_to,
                                          agg_from=agg_from,
                                          proc=p,
                                          local_exch=local_i,
                                          local_from=local_from,
                                          local_to=local_to)
                else:
                    # do we care about either of these, and do they map
                    # to different aggregated volumes?
                    agg_from,agg_to=self.seg_local_to_agg[p,pointers[local_i,:2]-1]

                    # -1 => not in an aggregation segment

                    if agg_from==-1 and agg_to==-1:
                        # between two segments we aren't tracking
                        continue 
                    elif agg_from==agg_to:
                        # within the same segment - fluxes cancel
                        continue
                    elif agg_from==-1 or agg_to==-1:
                        # agg boundary - have to manufacture boundary here
                        # currently, not explicitly recording the boundary segment
                        # info here, so when aggregated bcs are created, that info
                        # will not be packaged in the same way as bringing unaggregated
                        # boundary data into aggregated boundary data.
                        self.reg_agg_exch(direc=direc,
                                          agg_to=agg_to,
                                          agg_from=agg_from,
                                          proc=p,
                                          local_exch=local_i,
                                          local_from=local_from,
                                          local_to=local_to)
                    else:
                        if 1: # new style
                            self.reg_agg_exch(direc=direc,
                                              agg_from=agg_from,
                                              agg_to=agg_to,
                                              proc=p,
                                              local_exch=local_i,
                                              local_from=local_from,
                                              local_to=local_to)
                        else:
                            # this is a linkage which crosses between aggregated segments.
                            # the simplest real case
                            sel_fwd=(self.agg_exch['from']==agg_from)&(self.agg_exch['to']==agg_to)
                            sel_rev=(self.agg_exch['from']==agg_to)  &(self.agg_exch['to']==agg_from)
                            idxs_fwd=np.nonzero(sel_fwd)[0]
                            idxs_rev=np.nonzero(sel_rev)[0]

                            assert(len(idxs_fwd)+len(idxs_rev) == 1 )
                            if len(idxs_fwd):
                                self.exch_local_to_agg[p,local_i]=idxs_fwd[0]
                                self.exch_local_to_agg_sgn[p,local_i]=1
                            else:
                                self.exch_local_to_agg[p,local_i]=idxs_rev[0]
                                self.exch_local_to_agg_sgn[p,local_i]=-1

    def reg_agg_exch(self,direc,agg_from,agg_to,proc,local_exch,local_from,local_to):
        if agg_from is BOUNDARY: # was a boundary exchange in the unaggregated grid
            # the difference between this and the cases below is that here we want
            # to remember the index of the local bc, and associate it with
            # the aggregated BC.

            # if self.agg_boundaries is True, then            
            # all boundary exchanges for this agg_to segment map to a single
            # aggregated exchange.
            # if self.agg_boundaries is False, then repeated calls to get_agg_exchange
            # with the same agg_to create and return multiple, distinct boundary exchanges.
            
            # the -999 isn't really used beyond checking the sign, but there
            # to aid debugging.
            # 
            agg_exch_idx,sgn=self.get_agg_exchange(-999,agg_to)

            # dropped bc_local_to_agg cruft from here.
        else:
            # get_agg_exchange handles whether agg_from or agg_to are negative
            # indicating a boundary
            agg_exch_idx,sgn=self.get_agg_exchange(agg_from,agg_to)

        self.exch_local_to_agg[proc,local_exch]=agg_exch_idx
        self.exch_local_to_agg_sgn[proc,local_exch]=sgn
        
    def reindex(self):
        """
        take the list versions of agg_exch and agg_seg, sort 
        in a reasonable way, and update indices accordingly.

        updates:
        agg_seg
        agg_exch
        bc_...
        agg_seg_hash
        agg_exch_hash
        ...
        """
        # keep anybody from using these - may have to 
        # repopulate depending...
        self.agg_seg_hash=None
        self.agg_exch_hash=None

        # presumably agg_seg and agg_exch start as lists - normalize
        # to arrays for reindexing
        agg_seg=np.asarray(self.agg_seg,dtype=self.agg_seg_dtype)
        agg_exch=np.asarray(self.agg_exch,dtype=self.agg_exch_dtype)

        # seg_order[0] is the index of the original segments which
        # comes first in the new order.
        seg_order=np.lexsort( (agg_seg['elt'], agg_seg['k']) )
        agg_seg=agg_seg[seg_order]
        # seg_mapping[0] gives the new index of what used to be index 0
        seg_mapping=utils.invert_permutation(seg_order)
        self.agg_seg=agg_seg

        # update seg_local_to_agg
        sel=self.seg_local_to_agg>=0
        self.seg_local_to_agg[sel]=seg_mapping[ self.seg_local_to_agg[sel] ]

        # update from/to indices in exchanges:
        sel=agg_exch['from']>=0
        agg_exch['from'][sel] = seg_mapping[ agg_exch['from'][sel] ]
        # this used to be >0.  Is it possible that was the source of strife?
        sel=agg_exch['to']>=0
        agg_exch['to'][sel] =   seg_mapping[ agg_exch['to'][sel] ]
        self.n_agg_segments=len(self.agg_seg)

        # lexsort handling of boundary segments: 
        # should be okay - they will be sorted to the beginning of each layer

        exch_order=np.lexsort( (agg_exch['from'],agg_exch['to'],agg_exch['k'],agg_exch['direc']) )
        agg_exch=agg_exch[exch_order]
        exch_mapping=utils.invert_permutation(exch_order) 

        sel=self.exch_local_to_agg>=0
        self.exch_local_to_agg[sel]=exch_mapping[self.exch_local_to_agg[sel]]

        # bc_local_to_agg - excised

        self.n_exch_x=np.sum( agg_exch['direc']==b'x' )
        self.n_exch_y=np.sum( agg_exch['direc']==b'y' )
        self.n_exch_z=np.sum( agg_exch['direc']==b'z' )

        # used to populate agg_{x,y,z}_exch, too.
        self.agg_exch=agg_exch # used to be self.agg_exch

        # with all the exchanges in place and ordered correctly, assign boundary segment
        # indices to boundary exchanges
        n_bdry_exch=0
        for exch in self.agg_exch:
            if exch['from']>=0:
                continue # skip internal
            assert(exch['from']==REINDEX) # sanity, make sure nothing is getting mixed up.
            n_bdry_exch+=1
            # these have to start from -1, going negative
            exch['from']=-n_bdry_exch
       
        self.log.info("Aggregated output will have" )
        self.log.info(" %5d segments"%(self.n_agg_segments))
        self.log.info(" %5d exchanges (%d,%d,%d)"%(len(self.agg_exch),
                                                   self.n_exch_x,self.n_exch_y,self.n_exch_z) )
        
        self.log.info(" %5d boundary exchanges"%n_bdry_exch)

        
    lookup=None
    def elt_to_elt_length(self,elt_a,elt_b):
        if self.lookup is None:
            self.lookup={}

        key=(elt_a,elt_b) 
        if key not in self.lookup:
            if elt_a<0:
                a_len=b_len=np.sqrt(self.elements['poly'][elt_b].area)/2.
            elif elt_b<0:
                a_len=b_len=np.sqrt(self.elements['poly'][elt_a].area)/2.
            else:
                apoly=self.elements['poly'][elt_a]
                bpoly=self.elements['poly'][elt_b]

                buff=1.0 # geos => sloppy intersection test
                iface=apoly.buffer(buff).intersection(bpoly.buffer(buff))
                iface_length=iface.area/(2*buff) # verified

                # rough scaling of distance from centers to interface - seems reasonable.
                a_len=apoly.area / iface_length / 2.
                b_len=bpoly.area / iface_length / 2.
            self.lookup[key]=(a_len,b_len)
        return self.lookup[key]

    def add_exchange_data(self):
        """ Fill in extra details about exchanges, e.g. length scales
        """
        for exch in self.agg_exch:
            if exch['direc']==b'x':
                flen,tlen=self.elt_to_elt_length(exch['from_2d'],exch['to_2d'])
                exch['from_len']=flen
                exch['to_len']  =tlen
            elif exch['direc']==b'z':
                # not entirely sure about this...
                exch['from_len']=0.5
                exch['to_len']=0.5

    def get_agg_segment(self,agg_k,agg_elt):
        """ used to be agg_linear_map.
        on-demand allocation/temporary indexing for segments.
        
        for aggregated segments, map [layer,element] indices to a linear
        index.  This will only be dense if all elements have the maximum
        number of segments - otherwise some sorting/subsetting afterwards is
        likely necessary.
        """
        # outer loop is vertical, fastest varying index is horizontal
        # map [agg_horizontal_cell,k_from_top] to [agg_element_id]
        # n_agg_layers is the *max* number of layers in an aggregated element

        #if not self.sparse_layers:
        # #old static approach:
        # return agg_elt+agg_k*self.n_agg_elements_2d

        # even with dense layers, use this so that the implementation doesn't
        # fracture too much
        key=(agg_k,agg_elt)
        if key not in self.agg_seg_hash:
            idx=len(self.agg_seg)
            seg=np.zeros( (), dtype=self.agg_seg_dtype)
            seg['k']=agg_k
            seg['elt']=agg_elt
            seg['active']=True # default, but caller can overwrite
            self.agg_seg.append(seg)
            self.agg_seg_hash[key]=idx
        else:
            idx=self.agg_seg_hash[key]
        return idx

    def get_agg_exchange(self,agg_from,agg_to,direc=None):
        """
        return a [temporary] linear index and sign for the requested exchange.
        if there is already an exchange going the opposite direction,
        returns index,-1 to indicate that the sign is reversed, otherwise,
        index,1.
        either agg_from or agg_to can be negative - the value will be replaced with
        REINDEX
        (had tried preserving...
        but does not affect lookups (i.e. f(-1,2) and f(-2,2) will return the 
        same exchange, which will reflect the value of agg_from in the first call).
        )

        direc is typically inferred based on whether agg_from and agg_to are in the
        same element (direc<-'z').

        2016-07-18: behavior is modified by self.agg_boundaries.  if False, then
        unique exchanges are returned for multiple calls with the same agg_from<0 and
        agg_to.
        """
        def create_exch(a_from,a_to):
            """ 
            given the aggregated from/to segments, fill in some other useful
            exchange fields, notably from_2d/to_2d, direc, k, and set lengths to nan
            """
            exch=np.zeros( (), dtype=self.agg_exch_dtype)
            k=None
            if a_from>=0:
                exch['from_2d']=self.agg_seg[a_from]['elt']
                k=self.agg_seg[a_from]['k']
            else:
                exch['from_2d']=-1

            if a_to>=0:
                exch['to_2d']=self.agg_seg[a_to]['elt']
                if k is None:
                    k=self.agg_seg[a_to]['k']
            else:
                exch['to_2d']=-1

            if exch['from_2d']==exch['to_2d']:
                exch['direc']  = b'z'
            else:
                exch['direc'] = b'x' 

            exch['from']=a_from
            exch['to']=a_to
            exch['k']=k

            # filled in later:
            exch['from_len']=np.nan
            exch['to_len']=np.nan

            self.agg_exch.append(exch)
            idx=len(self.agg_exch)-1
            return idx
            
        # special handling for agg_from<0, indicating a boundary exchange
        # indexed only by the internal, agg_to index.
        if agg_from<0:
            agg_from=REINDEX
            if self.agg_boundaries:
                if agg_to not in self.agg_exch_hash:
                    idx=create_exch(agg_from,agg_to)
                    self.agg_exch_hash[agg_to]=idx
                else:
                    idx=self.agg_exch_hash[agg_to]
            else:
                # when aggregating these, can just index it by agg_to.
                # it's important here that there aren't extraneous calls
                # to get_agg_exchange or reg_exchange.

                # so we always create an exchange -
                idx=create_exch(agg_from,agg_to)

                # and scan for increasingly negative numbers for it's
                # hash.  HERE: what are the expectations on self.agg_exch_hash
                # outside of this function?

                # when *not* aggregating, index non-aggregated boundary exchanges
                # by a negative count - which starts -10000 as a bit of a hint
                # when things go south
                count=-10000
                while (count,agg_to) in self.agg_exch_hash:
                    count-=1
                self.agg_exch_hash[(count,agg_to)]=idx
            sgn=1
        elif agg_to<0: # does this happen?
            self.log.warning("get_exchange with a negative/boundary for the *to* segment")
            assert self.agg_boundaries # if this is a problem, port the above stanza to here.
            agg_to=REINDEX
            if agg_from not in self.agg_exch_hash:
                idx=create_exch(agg_to,agg_from)
                self.agg_exch_hash[agg_from]=idx
            sgn=-1
            idx=self.agg_exch_hash[agg_from]
        elif (agg_to,agg_from) in self.agg_exch_hash:
            # regular exchange, but reversed from how we already have it.
            sgn=-1
            idx=self.agg_exch_hash[ (agg_to,agg_from) ]
        else:
            if (agg_from,agg_to) not in self.agg_exch_hash:
                # create new exchange
                idx=create_exch(agg_from,agg_to)
                self.agg_exch_hash[(agg_from,agg_to)]=idx
            else:
                # this exact exchange already exists
                idx=self.agg_exch_hash[ (agg_from,agg_to) ]
            sgn=1

        return idx,sgn

    _pointers=None
    @property
    def pointers(self):
        if self._pointers is None:
            pointers=np.zeros( (len(self.agg_exch),4),'i4')
            bc=(self.agg_exch['from']<0)
            pointers[:,0]=self.agg_exch['from'] 
            pointers[~bc,0] += 1 # internal exchanges should use 1-based index
            # pointers[bc,0] - reindex() already has the right numbering for these
            pointers[:,1]=self.agg_exch['to']   + 1
            pointers[:,2:]=self.CLOSED # no support for higher order advection
            self._pointers=pointers

        return self._pointers

    @property
    def exchange_lengths(self):
        exchange_lengths=np.zeros( (len(self.agg_exch),2),'f4')
        exchange_lengths[:,0]=self.agg_exch['from_len']
        exchange_lengths[:,1]=self.agg_exch['to_len']
        return exchange_lengths

    @property
    def time0(self):
        return self.open_hyd(0).time0

    @property
    def t_secs(self):
        return self.open_hyd(0).t_secs

    def init_seg_matrices(self):
        """ initialize dict:
        self.seg_matrix
        which maps processor ids to sparse matrices, which can be
        left multiplied with a per-processor vector:
          E.dot(local_seg_value) => agg_seg_value
        as a sum of local_seg_value
        """
        self.seg_matrix={}


        for p in range(self.nprocs):
            # HERE: probably this is where we need to 
            # additionally limit sel to segments local to p (non-ghost)
            sel=(self.seg_local_to_agg[p,:]>=0)
            if not np.any(sel):
                self.seg_matrix[p] = None
                continue

            nc=self.open_flowgeom(p) 
            dom_id=nc.FlowElemDomain[:]
            hyd=self.open_hyd(p)
            hyd.infer_2d_elements()

            # expand domain info to 3D segments
            local_dom=dom_id[hyd.seg_to_2d_element] # now in 3D
            # sel is much bigger than local_dom, but numpy silently allows it
            # actually newer numpy complains
            # so use the fact that the first slice is a view, and we can assign
            # to a bitmask selection.
            sel[:len(local_dom)][local_dom!=p] = False

            rows=self.seg_local_to_agg[p,sel]
            cols=np.nonzero(sel)[0]
            vals=np.ones_like(cols)

            S=sparse.coo_matrix( (vals, (rows,cols)),
                                 (self.n_seg,hyd.n_seg),dtype='f4')
            self.seg_matrix[p]=S.tocsr()
            
    def init_exch_matrices(self):
        """ initialize dicts:
        self.flow_matrix, self.area_matrix
        which maps processor ids to sparse matrices, which can be
        left multiplied with a per-processor vector:
          E.dot(local_flow) => agg_flow
        or 
          E.dot(local_area) => agg_area
        The difference between flow and area being that flow is signed
        while area is unsigned.
        """
        self.flow_matrix={}
        self.area_matrix={}

        for p in range(self.nprocs):
            hyd=self.open_hyd(p)
            n_exch_local=hyd['number-horizontal-exchanges']+hyd['number-vertical-exchanges']

            exch_local_to_agg=self.exch_local_to_agg[p,:]
            idxs=np.nonzero( exch_local_to_agg>=0 )[0]

            if len(idxs)==0:
                continue
            assert( idxs.max() < n_exch_local )
            rows=exch_local_to_agg[idxs]
            cols=idxs
            values=self.exch_local_to_agg_sgn[p,idxs]

            Eflow=sparse.coo_matrix( (values, (rows,cols)),
                                     (self.n_exch,n_exch_local),dtype='f4')
            self.flow_matrix[p]=Eflow.tocsr()

            # areas always sum
            Earea=sparse.coo_matrix( (np.abs(values), (rows,cols)),
                                     (self.n_exch,n_exch_local),dtype='f4')
            self.area_matrix[p]=Earea.tocsr()

    def init_boundary_matrices(self):
        """ populates:
          self.bc_local_segs={} # bc_segs[proc] => [0-based seg indices] 
          self.bc_local_exchs={}
          self.bc_exch_local_to_agg={}

        local_seg_scalar[bc_local_segs[p]] gives the subset of segment
        concentrations on proc p which appear at aggregated boundaries.
        
        local_exch_area[bc_local_exchs[p]] gives the corresponding area
        of exchanges which span aggregated boundaries.
        
        bc_exch_local_to_agg is a matrix, E.dot(bc_values) aggregates local
        values from above to aggregated boundaries.
        """

        # E.dot(seg_values) => sum of segment values adjacent to aggregated boundary
        # not quite right for BCs, since we really want to be weighing by exchange
        # area.

        self.bc_seg_matrix=bc_seg_matrix={} 

        self.bc_local_segs={} # bc_segs[proc] => [0-based seg indices] 
        self.bc_local_exchs={}
        self.bc_exch_local_to_agg={}

        warned_internal_boundary_seg=False # control one-off warning below

        for p in range(self.nprocs): # following exch and area matrix code
            # set defaults, so if no exchanges map to this processor, just
            # bail on the loop
            self.bc_seg_matrix[p]=None
            self.bc_local_segs[p]=None
            self.bc_local_exchs[p]=None
            self.bc_exch_local_to_agg[p]=None

            hyd=self.open_hyd(p)
            n_seg_local=hyd.n_seg

            exch_local_to_agg=self.exch_local_to_agg[p,:] # 0-based
            idxs=np.nonzero( exch_local_to_agg>=0 )[0]

            if len(idxs)==0: 
                continue

            local_bc_segs=[] # indices
            local_bc_exchs=[] # indices into local exchanges
            # row is aggregated bc exch, col is local_bc_exch

            # count number of local bc exchanges a priori
            n_local_bc_exch = np.sum(  self.pointers[exch_local_to_agg[idxs],0]<0 )
            #                                                         ^ narrow to boundary exchs
            #                                        ^ index of agg. exch. for each local exch
            #                         # ^ get the aggregated 'from' segment 
            #                                                                  ^ test for it being an agg bc

            local_bc_to_agg=sparse.dok_matrix( (self.n_boundaries,n_local_bc_exch) )

            rows=[] # index of aggregated boundary exchanges - 0-based
            cols=[] # index of local segment - 0-based
            vals=[] # just 1 or 0

            # indices of aggregated exchanges which are part of the boundary
            agg_bc_exchanges=np.nonzero(self.pointers[:,0]<0)[0] # 0-based

            # the ordering in pointers (-1,-2,-3,...) should match the ordering
            # of boundaries in the input file
            local_pointers=hyd.pointers
            for j,agg_exch_idx in enumerate(agg_bc_exchanges):
                # j: 0-based index of aggregated boundary exchanges, i.e. BCs.
                # agg_exch_idx: 0-based index of that exchange

                # find local exchanges which map to this aggregated exchange:
                local_exch_match_idxs=np.nonzero( exch_local_to_agg==agg_exch_idx )[0]

                for local_exch_idx in local_exch_match_idxs:
                    seg1,seg2=local_pointers[local_exch_idx,:2] # 1-based!
                    assert(seg1!=0) # paranoid
                    assert(seg2!=0) # paranoid

                    # if one is negative, it's a boundary and it's either on a different
                    # processor or was a boundary in the unaggregated domain

                    # we want a way to find out the aggregated boundary condition,
                    # but if one segment is negative, we don't have the data, and
                    # have to settle for grabbing data from the internal segment
                    # as being representative of the boundary condition.

                    # in cases where the exchange represents an unaggregated boundary
                    # exchange, then there is the opportunity to assign boundary conditions
                    # based on original forcing data.  not sure if this really ought to
                    # be a warning

                    if seg1<0:
                        if not warned_internal_boundary_seg:
                            self.log.info("had to choose internal segment for agg boundary")
                            warned_internal_boundary_seg=True
                        local_seg=seg2
                    elif seg2<0:
                        if not warned_internal_boundary_seg:
                            self.log.info("had to choose internal segment for agg boundary")
                            warned_internal_boundary_seg=True
                        local_seg=seg1
                    elif self.seg_local_to_agg[p,seg1-1]>=0:
                        # try to get the segment which is exterior to the aggregated segment
                        assert self.seg_local_to_agg[p,seg2-1]<0
                        local_seg=seg2
                    elif self.seg_local_to_agg[p,seg2-1]>=0:
                        assert self.seg_local_to_agg[p,seg1-1]<0
                        local_seg=seg1
                    else:
                        self.log.error("Boundary exchange had local exch where neither local segment is internal" )
                        assert False

                    # record these for the somewhat defunct bc_seg_matrix
                    rows.append(j) # which aggregated boundary exchanges is involved
                    cols.append(local_seg-1)
                    vals.append(1)

                    # record this for the more complex but correct local_bc_exch 
                    local_bc_segs.append(local_seg)
                    local_bc_exchs.append(local_exch_idx)
                    local_bc_to_agg[j,len(local_bc_exchs)-1]=1

            if len(rows):
                Eboundary=sparse.coo_matrix( (vals, (rows,cols)),
                                             (self.n_boundaries,n_seg_local),dtype='f4')
                self.bc_seg_matrix[p]=Eboundary.tocsr()

                self.bc_local_segs[p]       =np.array(local_bc_segs)
                self.bc_local_exchs[p]      =np.array(local_bc_exchs)
                self.bc_exch_local_to_agg[p]=local_bc_to_agg.tocsr()

                assert(n_local_bc_exch)
            else:
                if n_local_bc_exch:
                    print( "WARNING: a priori test showed that there should be local bc exchanges!")
                    print( "  Processor: %d"%p)

    def boundary_values(self,t_sec,label):
        """ Aggregate boundary condition data - segment data are pulled
        from each processor by reading the given label from the hyd files.
        """
        # follow logic similar to aggregated areas calculation
        areas=np.zeros(self.n_boundaries,'f4')
        aC_products=np.zeros(self.n_boundaries,'f4')

        for p,Eboundary in iteritems(self.bc_exch_local_to_agg):
            if Eboundary is None:
                continue
            hyd=self.open_hyd(p)
            p_bc_area=hyd.areas(t_sec)[self.bc_local_exchs[p]]
            p_bc_conc=hyd.seg_func(t_sec,label=label)[self.bc_local_segs[p]]

            areas       += Eboundary.dot(p_bc_area)
            aC_products += Eboundary.dot(p_bc_area*p_bc_conc)
        sel=(areas!=0.0)
        aC_products[sel] = aC_products[sel]/areas[sel]
        aC_products[~sel] = -999
        return aC_products

    def volumes(self,t_sec,explicit=False):
        if not explicit: # much faster on dense outputs
            return self.segment_aggregator(t_sec=t_sec,
                                           seg_fn=lambda _: 1.0,
                                           normalize=False)
        else:
            # original, explicit version.  Slow!
            # retained for debugging, eventually remove.
            agg_vols=np.zeros(self.n_agg_segments,'f4')

            # loop on processor:
            for p in range(self.nprocs):
                hydp=self.open_hyd(p)

                vols=None

                # inner loop on target segment
                # this is the slow part!
                for agg_seg in range(self.n_agg_segments):
                    sel_3d=self.seg_local_to_agg[p,:]==agg_seg
                    if np.any(sel_3d):
                        if vols is None:
                            vols=hydp.volumes(t_sec)
                        # print "Found %d 3D segments which map to aggregated segment"%np.sum(sel_3d)
                        # sel_3d is "ragged", but stored rectangular.  trim it to avoid numpy 
                        # warnings
                        sel_3d=sel_3d[:len(vols)]
                        agg_vols[agg_seg]+= np.sum(vols[sel_3d])
            return agg_vols

    # delwaq doesn't tolerate any zero area exchanges - at least I think not.
    # a little unsure of how zero areas in the unfiltered data might have
    # worked.  
    exch_area_min=1.0
    exch_z_area_constant=True # if true, force all segment in a column to have same plan area.

    warned_forcing_constant_area=False
    def areas(self,t):
        areas=np.zeros(self.n_exch,'f4')
        for p,Earea in iteritems(self.area_matrix):
            hyd=self.open_hyd(p)
            p_area=hyd.areas(t)
            areas += Earea.dot(p_area)
        # try re-introducing this line... had coincided with this setup breaking
        # okay - that ran okay..  but it ran okay without this line, so
        # maybe nix it?
        # areas[areas<self.exch_area_min]=self.exch_area_min
        # trying to reintroduce this line... seemed okay

        # no longer trying to do wacky things with area, maybe okay to drop this
        # self.monotonicize_areas(areas)

        # fast forward to 2016-07-22: pretty sure we need the planform area to be
        # constant through the water column.  I thought that was already in place, but
        # the output shows that's not the case.
        # if we are only merging, then assume that the data coming in already
        # has constant areas in the vertical (i.e. it's original hydro cells which
        # don't have any partial areas
        if self.exch_z_area_constant:
            if not self.warned_forcing_constant_area:
                self.warned_forcing_constant_area=True
                self.log.warning('Forcing constant area within water column')
            self.monotonicize_areas(areas)
            self.monotonicize_areas(areas,top_down=True)
        return areas

    def monotonicize_areas(self,areas,top_down=False):
        """ areas: n_exch * 'f4'
        Modify areas so that vertical exchange areas are monotonically 
        decreasing.
        by default, this means starting at the bottom of the water column
        and make sure that areas are non-decreasing as we go up.  but it can
        also be called with top_down=True, to do the opposite.  This is mostly
        just useful to make the area constant in the entire water column
        """
        # self.log.info("Call to monotonicize areas!")
        # this looks very slow.
        seg_A=np.zeros(self.n_seg)
        pointers=self.pointers
        js=np.arange(self.n_exch-self.n_exch_z,self.n_exch)
        if not top_down:
            js=js[::-1]
        for j in js:
            top,bot = pointers[j,:2] - 1
            if not top_down:
                if bot>=0: # update exchange from segment below
                    areas[j]=max(areas[j],seg_A[bot])
                if top>=0: # update segment above
                    seg_A[top]=max(seg_A[top],areas[j])
            else:
                if top>=0: # update exchange from the segment above
                    areas[j]=max(areas[j],seg_A[top])
                if bot>=0: # update segment below
                    seg_A[bot]=max(seg_A[bot],areas[j])
    
    def flows(self,t):
        """ 
        returns flow rates ~ np.zeros(self.n_exch,'f4'), for given timestep.
        flows in m3/s.
        """
        flows=np.zeros(self.n_exch,'f4')
        for p,Eflow in iteritems(self.flow_matrix):
            hyd=self.open_hyd(p)
            p_flow=hyd.flows(t)
            flows += Eflow.dot(p_flow)
        return flows

    def segment_aggregator(self,t_sec,seg_fn,normalize=True,min_volume=0.00001):
        """ 
        Generic segment scalar aggregation
        t_sec: simulation time, integer seconds
        seg_fn: lambda proc => scalar for each unaggregated segment
        normalize: if True, divide by aggregated volume, otherwise just sum
        min_volume: if normalizing by volume, this volume is added, so that zero-volume
        inputs with valid scalars will produce valid output.  Note that this included 
        for all unaggregated segments - in cases where there are large numbers of 
        empty segments aggregated with a few small non-empty segments, then there will
        be some error.  but min_volume can be very small
        """
        # volume-weighted averaging
        agg_scalars=np.zeros(self.n_seg,'f4')
        agg_volumes=np.zeros(self.n_seg,'f4')

        # loop on processor:
        for p in range(self.nprocs):
            if np.all(self.seg_local_to_agg[p,:]<0):
                continue

            hydp=self.open_hyd(p)
            vols=hydp.volumes(t_sec)
            if min_volume>0:
                vols=vols.clip(min_volume,np.inf)
            scals=seg_fn(p) * np.ones_like(vols) # mult in case seg_fn returns a scalar

            # inner loop on target segment
            S=self.seg_matrix[p]
            agg_scalars += S.dot( vols*scals )
            agg_volumes += S.dot( vols )

        if normalize:
            valid=agg_volumes>0
            agg_scalars[valid] /= agg_volumes[valid]
            agg_scalars[~valid] = 0.0
        return agg_scalars

    def seg_func(self,t_sec=None,label=None,param_name=None):
        """ return a callable which implements a segment function using data
        from unaggregated files, either with the given label mentioned in the
        hyd file (i.e. label='salinity-file'), or by grabbing a parameter 
        of a given name.

        if t_sec is given, evaluate at that time and return the result
        """
        def f_label(t,label=label):
            return self.segment_aggregator(t,
                                           lambda proc: self.open_hyd(proc).seg_func(t,label=label),
                                           normalize=True)
        def f_param(t,param_name=param_name):
            per_proc=lambda proc: self.open_hyd(proc).parameters(force=False)[param_name].evaluate(t=t).data
            return self.segment_aggregator(t,per_proc,normalize=True)
        if param_name:
            f=f_param
        elif label:
            f=f_label
        else:
            raise Exception("One of label or param_name must be supplied")
            
        if t_sec is not None:
            return f(t_sec)
        else:
            return f

    def vert_diffs(self,t_sec):
        # returns [n_segs]*'f4' vertical diffusivities in m2/s
        # based on the output from Rose, the top layer is missing
        # vertical diffusivity entirely.
        diffs=self.segment_aggregator(t_sec,
                                       lambda proc: self.open_hyd(proc).vert_diffs(t_sec),
                                       normalize=True)
        # kludge - get a nonzero diffusivity in the top layer
        n2d=self.n_2d_elements
        diffs[:n2d] = diffs[n2d:2*n2d]
        return diffs

    def planform_areas(self):
        """ 
        Return a Parameter object encapsulating variability of planform 
        area.  Typically this is a per-segment, constant-in-time 
        parameter, but it could be globally constant or spatially and 
        temporally variable.
        Old interface returned Nsegs * 'f4', which can be recovered 
        in simple cases by accessing the .data attribute of the
        returned parameter

        HERE: when bringing in z-layer data, this needs some extra 
        attention.  In particular, planform area needs to be (i) the same 
        for all layers, and (ii) should be chosen to preserve average depth,
        presumably average depth of the wet part of the domain?
        """
        # prior to 4/27/16 this was set to lazy.  but with 1:1 mapping that
        # was leading to bad continuity results.  overall, seems like we should
        # stick with constant.
        # mode='lazy'
        mode='constant'
        # mode='explicit'

        min_planform_area=1.0

        if mode is 'constant':
            #switching to the code below coincided with this setup breaking
            # This is the old code - just maps maximum area from the grid
            map2d3d=self.infer_2d_elements() # agg_seg_to_agg_elt_2d()
            data=(self.elements['plan_area'][map2d3d]).astype('f4')
            return ParameterSpatial(data,hydro=self)
        else: # new code, copied from FilteredBC
            # pull areas from exchange areas of vertical exchanges

            seg_z_exch=self.seg_to_exch_z(preference='upper')

            missing= (seg_z_exch<0)
            if np.any(missing):
                self.log.warning("Some segments have no vertical exchanges - will revert to element area")
                map2d3d=self.infer_2d_elements() 
                constant_areas=(self.elements['plan_area'][map2d3d]).astype('f4')
            else:
                constant_areas=None

            if mode is 'lazy':
                def planform_area_func(t_sec):
                    A=np.zeros(self.n_seg,'f4')
                    areas=self.areas(t_sec)
                    if constant_areas is None:
                        A[:]=areas[seg_z_exch]
                    else:
                        A[~missing]=areas[seg_z_exch[~missing]]
                        A[missing] =constant_areas[seg_z_exch[missing]]
                    A[ A<min_planform_area ] = min_planform_area
                    return A
                return ParameterSpatioTemporal(func_t=planform_area_func,
                                               times=self.t_secs,hydro=self)

            else: # 'explicit'
                # then pull exchange area for each time step
                A=np.zeros( (len(self.t_secs),self.n_seg) )
                for ti,t_sec in enumerate(self.t_secs):
                    areas=self.areas(t_sec)
                    A[ti,~missing] = areas[seg_z_exch[~missing]]
                    A[ti,missing]=constant_areas[missing]

                # without this, but with zero area exchanges, and monotonicize
                # enabled, it was crashing, complaining that DDEPTH ran into
                # zero SURF.
                # enabling this lets it run, though depths are pretty wacky.
                A[ A<min_planform_area ] = min_planform_area

                return ParameterSpatioTemporal(times=self.t_secs,values=A,hydro=self)

    def depths(self):
        """ Temporarily copied from FilteredBC
        Compute time-varying segment thicknesses.  With z-levels, this is
        a little more nuanced than the standard calc. in delwaq.

        It uses a combination of planform area and vertical exchange area
        to get depths.  
        """
        mode='lazy'
        # mode='explicit'

        min_depth=0.001

        # use upper, since some bed segment would have a zero area for the
        # lower exchange
        self.log.debug("Call to WaqAggregator::depth()")

        # used to duplicate some of the code in planform_areas, grabbing
        # exchange areas and mapping the vertical exchanges to segments
        # should be fine to delegate that
        plan_areas=self.planform_areas()

        #seg_z_exch=self.seg_to_exch_z(preference='upper')
        #assert np.all(seg_z_exch>=0) # could be a problem if an element has 1 layer
        def clean_depth(data):
            """ fix up nan and zero depth values in place.
            """
            sel=(~np.isfinite(data))
            if np.any(sel):
                self.log.warning("Depths: %d steps*segments with invalid depth"%( np.sum(sel) ))
            data[sel]=0

            # seems that this is necessary.  ran okay with 0.01m
            data[ data<min_depth ] = min_depth

        if mode is 'lazy':
            def depth_func(t_sec):
                # a little unsure on the .data part
                D=self.volumes(t_sec) / plan_areas.evaluate(t=t_sec).data
                clean_depth(D)
                return D.astype('f4')
            return ParameterSpatioTemporal(times=self.t_secs,func_t=depth_func,hydro=self)
                
        if mode is 'explicit':
            D=np.zeros( (len(self.t_secs),self.n_seg) )
            for ti,t_sec in enumerate(self.t_secs):
                areas=self.areas(t_sec)
                volumes=self.volumes(t_sec)
                D[ti,:] = volumes / plan_areas.evaluate(t=t_sec).data

            clean_depth(D)
            return ParameterSpatioTemporal(times=self.t_secs,values=D,hydro=self)
        assert False

    def bottom_depths(self):
        """ 
        Like planform_areas, but for bottom depth.
        old interface: return Nsegs * 'f4' 
        """
        map2d3d=self.infer_2d_elements() # agg_seg_to_agg_elt_2d()
        data=(self.elements['zcc'][map2d3d]).astype('f4')
        return ParameterSpatial(data,hydro=self)

    def check_boundary_assumptions(self):
        """
        checks that boundary segments and exchanges obey some assumed
        invariants:
         - the boundary segment always appears first in exchanges
         - pointers show only horizontal exchanges having boundary segments
         - flowgeom shows that for boundary exchanges, the local/nonlocal status
           of the internal segment determines the local/nonlocal status of the
           exchange.
        """
        for p in range(self.nprocs):
            #print "------",p,"------"
            nc=self.open_flowgeom(p)
            hyd=self.open_hyd(p)
            poi=hyd.pointers
            n_layers=hyd['number-water-quality-layers']

            for ab in [0,1]:
                poi_bc_segs=poi[:,ab]<0
                idxs=np.nonzero(poi_bc_segs)[0]
                assert( np.all(idxs<hyd['number-horizontal-exchanges']) )
                idxs=idxs[:(len(idxs)/n_layers)]
                #print "poi[%d]: "%ab
                #print np.array( [idxs,poi[idxs,ab]] ).T
                if ab==1:
                    assert(len(idxs)==0)

            link=nc.FlowLink[:]
            link_domain=nc.FlowLinkDomain[:]
            elem_domain=nc.FlowElemDomain[:]
            nelems=len(elem_domain)

            for ab in [0,1]:
                nc_bc_segs=link[:,ab]>nelems
                idxs=np.nonzero(nc_bc_segs)[0]

                other_elem_is_local=elem_domain[link[idxs,1-ab]-1]==p

                link_is_local=link_domain[idxs]==p

                if ab==1:
                    assert(len(idxs)==0)

                # print " nc[%d]:"%ab
                # print np.array( [idxs,
                #                  link[idxs,ab],
                #                  other_elem_is_local,
                #                  link_is_local] ).T
                assert( np.all(other_elem_is_local==link_is_local) )

    def ghost_segs(self,p):
        hyd=self.open_hyd(p)
        nc=self.open_flowgeom(p)
        sel_2d=(nc.FlowElemDomain[:]!=p)
        return np.tile(sel_2d,self.n_src_layers)

    # def agg_seg_to_agg_elt_2d(self):
    #     """ Array of indices mapping 0-based aggregated segments
    #     to 0-based aggregated 2D segments. 
    #     """
    #     self.log.warning('agg_seg_to_agg_elt_2d is deprecated in favor of infer_2d_elements')
    #     # old implementation which assumed constant segments/layer
    #     # return np.tile(np.arange(len(self.elements)),self.n_agg_layers)
    #     return self.infer_2d_elements()

    def add_parameters(self,hparams):
        hparams=super(DwaqAggregator,self).add_parameters(hparams)
        
        hyd0=self.open_hyd(0)
        # 2017-01-5: used to call temperature 'temperature', but for dwaq it should
        #            be temp.

        if 0: # old approach, reached into the files of the unaggregated runs
            # aside from reaching around the abstraction a bit, this also
            # suffers from using the wrong time steps, as it assumes that
            # self.t_secs applies to all parameters
            for label,pname in [('vert-diffusion-file','VertDisper'),
                                ('salinity-file','salinity'),
                                ('temperature-file','temp')]:
                # see if the first processor has it
                if hyd0[label]=='none':
                    continue
                fn=hyd0.get_path(label)
                if not os.path.exists(fn):
                    self.log.info("DwaqAggregator: seg function %s (label=%s) not found"%(fn,label))
                    continue
                hparams[pname]=ParameterSpatioTemporal(func_t=self.seg_func(label=label),
                                                       times=self.t_secs,
                                                       hydro=self)
        else: # new approach - use unaggregated parameter objects.
            hyd0_params=hyd0.parameters(force=False)

            # could also loop over the parameters that hyd0 has, and just be sure
            # step over the ones that will be replaced, like surf.
            
            for pname in ['VertDisper','salinity','temp']:
                # see if the first processor has it
                if pname in hyd0_params:
                    hyd0_param=hyd0_params[pname]
                    # in the past, used the label to grab this from each unaggregated
                    # source.
                    # now we use the parameter name
                    hparams[pname]=ParameterSpatioTemporal(func_t=self.seg_func(param_name=pname),
                                                           times=hyd0_param.times,
                                                           hydro=self)

        return hparams

    def write_hyd(self,fn=None):
        """ Write an approximation to the hyd file output by D-Flow FM
        for consumption by delwaq or HydroFiles
        respects scen_t_secs
        """
        # currently the segment names here are out of sync with 
        # the names used by write_parameters.
        #  this is relevant for salinity-file,  vert-diffusion-file
        #  maybe surfaces-file, depths-file.
        # for example, surfaces file is written as tbd-SURF.seg
        # but below we call it com-tbd.srf
        # maybe easiest to just change the code below since it's
        # already arbitrary
        fn=fn or os.path.join( self.scenario.base_path,
                               self.fn_base+".hyd")

        name=self.scenario.name

        dfmt="%Y%m%d%H%M%S"
        time_start = (self.time0+self.scen_t_secs[0]*self.scenario.scu)
        time_stop  = (self.time0+self.scen_t_secs[-1]*self.scenario.scu)
        timedelta = (self.t_secs[1] - self.t_secs[0])*self.scenario.scu
        timestep = timedelta_to_waq_timestep(timedelta)

        # some values just copied from the first subdomain
        hyd0=self.open_hyd(0)
        n_layers=hyd0['number-hydrodynamic-layers']
        assert hyd0['number-hydrodynamic-layers']==hyd0['number-water-quality-layers']

        # New code - maybe not right at all - same as Hydro.write_hyd
        if 'temp' in self.parameters():
            temp_file="'%s-temp.seg'"%name
        else:
            temp_file='none'
            
        lines=[
            "file-created-by  SFEI, waq_scenario.py",
            "file-creation-date  %s"%( datetime.datetime.utcnow().strftime('%H:%M:%S, %d-%m-%Y') ),
            "task      full-coupling",
            "geometry  unstructured",
            "horizontal-aggregation no",
            "reference-time           '%s'"%( self.time0.strftime(dfmt) ),
            "hydrodynamic-start-time  '%s'"%( time_start.strftime(dfmt) ),
            "hydrodynamic-stop-time   '%s'"%( time_stop.strftime(dfmt)  ),
            "hydrodynamic-timestep    '%s'"%timestep, 
            "conversion-ref-time      '%s'"%( self.time0.strftime(dfmt) ),
            "conversion-start-time    '%s'"%( time_start.strftime(dfmt) ),
            "conversion-stop-time     '%s'"%( time_stop.strftime(dfmt)  ),
            "conversion-timestep      '%s'"%timestep, 
            "grid-cells-first-direction       %d"%self.n_2d_elements,
            "grid-cells-second-direction          0",
            "number-hydrodynamic-layers          %s"%( n_layers ),
            "number-horizontal-exchanges      %d"%( self.n_exch_x ),
            "number-vertical-exchanges        %d"%( self.n_exch_z ),
            # little white lie.  this is the number in the top layer.
            # and no support for water-quality being different than hydrodynamic
            "number-water-quality-segments-per-layer       %d"%( self.n_2d_elements),
            "number-water-quality-layers          %s"%( n_layers ),
            "hydrodynamic-file        '%s'"%self.fn_base,
            "aggregation-file         none",
            # filename handling not as elegant as it could be..
            # e.g. self.vol_filename should probably be self.vol_filepath, then
            # here we could reference the filename relative to the hyd file
            "grid-indices-file     '%s.bnd'"%self.fn_base,# lies, damn lies
            "grid-coordinates-file '%s'"%self.flowgeom_filename,
            "attributes-file       '%s.atr'"%self.fn_base,
            "volumes-file          '%s.vol'"%self.fn_base,
            "areas-file            '%s.are'"%self.fn_base,
            "flows-file            '%s.flo'"%self.fn_base,
            "pointers-file         '%s.poi'"%self.fn_base,
            "lengths-file          '%s.len'"%self.fn_base,
            "salinity-file         '%s-salinity.seg'"%name,
            "temperature-file      %s"%temp_file,
            "vert-diffusion-file   '%s-vertdisper.seg'"%name,
            # not a segment function!
            "surfaces-file         '%s'"%self.surf_filename,
            "shear-stresses-file   none",
            "hydrodynamic-layers",
            "\n".join( ["%.5f"%(1./n_layers)] * n_layers ),
            "end-hydrodynamic-layers",
            "water-quality-layers   ",
            "\n".join( ["1.000"] * n_layers ),
            "end-water-quality-layers"]
        txt="\n".join(lines)
        with open(fn,'wt') as fp:
            fp.write(txt)

    @property
    def surf_filename(self):
        return self.fn_base+".srf"
    
    def write_srf(self):
        surfaces=self.elements['plan_area']
        # this needs to be in sync with what write_hyd writes, and
        # the supporting_file statement in the hydro_parameters
        fn=os.path.join(self.scenario.base_path,self.surf_filename)

        nelt=self.n_2d_elements
        with open(fn,'wb') as fp:
            # shape, shape, count, x,x,x according to waqfil.m
            hdr=np.zeros(6,'i4')
            hdr[0]=hdr[2]=hdr[3]=hdr[4]=nelt
            hdr[1]=1
            hdr[5]=0
            fp.write(hdr.tobytes())
            fp.write(surfaces.astype('f4'))

    def get_geom(self):
        ds=xr.Dataset()

        xycc = np.array( [poly.centroid.coords[0] for poly in self.elements['poly']] )

        ds['FlowElem_xcc']=xr.DataArray(xycc[:,0],dims=['nFlowElem'],
                                        attrs=dict(units='m',
                                                   standard_name = "projection_x_coordinate",
                                                   long_name = "Flow element centroid x",
                                                   bounds = "FlowElemContour_x",
                                                   grid_mapping = "projected_coordinate_system"))
        ds['FlowElem_ycc']=xr.DataArray(xycc[:,1],dims=['nFlowElem'],
                                  attrs=dict(units='m',
                                             standard_name = "projection_y_coordinate",
                                             long_name = "Flow element centroid y",
                                             bounds = "FlowElemContour_y",
                                             grid_mapping = "projected_coordinate_system"))

        ds['FlowElem_zcc']=xr.DataArray(self.elements['zcc'],dims=['nFlowElem'],
                                        attrs=dict(long_name = ("Flow element average"
                                                                " bottom level (average of all corners)"),
                                                   positive = "down",
                                                   mesh = "FlowMesh",
                                                   location = "face"))

        ds['FlowElem_bac']=xr.DataArray(self.elements['plan_area'],
                                        dims=['nFlowElem'],
                                        attrs=dict(long_name = "Flow element area",
                                                   units = "m2",
                                                   standard_name = "cell_area",
                                                   mesh = "FlowMesh",
                                        location = "face" ) )

        # make a ragged list first
        # but shapely repeats the first point, so shave that off
        poly_points = [np.array(p.exterior.coords)[:-1]
                       for p in self.elements['poly']]
        # also shapely may give the order CW
        for i in range(len(poly_points)):
            if utils.signed_area(poly_points[i])<0:
                poly_points[i] = poly_points[i][::-1]

        max_points=np.max([len(pnts) for pnts in poly_points])

        packed=np.zeros( (len(poly_points),max_points,2), 'f8')
        packed[:]=np.nan
        for pi,poly in enumerate(poly_points):
            packed[pi,:len(poly),:] = poly

        ds['FlowElemContour_x']=xr.DataArray(packed[...,0],
                                             dims=['nFlowElem','nFlowElemContourPts'],
                                             attrs=dict(units = "m",
                                                        standard_name = "projection_x_coordinate" ,
                                                        long_name = "List of x-points forming flow element" ,
                                                        grid_mapping = "projected_coordinate_system"))
        ds['FlowElemContour_y']=xr.DataArray(packed[...,1],
                                             dims=['nFlowElem','nFlowElemContourPts'],
                                             attrs=dict(units="m",
                                                        standard_name="projection_y_coordinate",
                                                        long_name="List of y-points forming flow element",
                                                        grid_mapping="projected_coordinate_system"))

        ds['FlowElem_bl']=xr.DataArray(-self.elements['zcc'],dims=['nFlowElem'],
                                       attrs=dict(units="m",
                                                  positive = "up" ,
                                                  standard_name = "sea_floor_depth" ,
                                                  long_name = "Bottom level at flow element\'s circumcenter." ,
                                                  grid_mapping = "projected_coordinate_system" ,
                                                  mesh = "FlowMesh",
                                                  location = "face"))

        sel = (self.agg_exch['direc']==b'x') & (self.agg_exch['k']==0)

        # use the seg from, not from_2d, because they have the real
        # numbering for the boundary exchanges (from_2d just has -1)
        links=np.array( [ self.agg_exch['from'][sel],
                          self.agg_exch['to'][sel] ] ).T
        bc=(links<0)
        links[bc]=self.n_2d_elements - links[bc] - 1
        bclinks=np.any(bc,axis=1)

        # 1-based
        ds['FlowLink']=xr.DataArray(links+1,dims=['nFlowLink','nFlowLinkPts'],
                                    attrs=dict(long_name="link/interface between two flow elements"))

        ds['FlowLinkType']=xr.DataArray(2*np.ones(len(links)),dims=['nFlowLink'],
                                        attrs=dict(long_name="type of flowlink",
                                                   valid_range="1,2",
                                                   flag_values="1,2",
                                                   flag_meanings=("link_between_1D_flow_elements "
                                                                  "link_between_2D_flow_elements" )))

        xyu=np.zeros((len(links),2),'f8')
        xyu[~bclinks]=xycc[links[~bclinks]].mean(axis=1) # average centroids
        xyu[bclinks]=xycc[links[bclinks].min(axis=1)] # centroid of real element

        ds['FlowLink_xu']=xr.DataArray(xyu[:,0],dims=['nFlowLink'],
                                       attrs=dict(units="m",
                                                  standard_name = "projection_x_coordinate" ,
                                                  long_name = "Center coordinate of net link (velocity point)." ,
                                                  grid_mapping = "projected_coordinate_system"))
        ds['FlowLink_yu']=xr.DataArray(xyu[:,1],dims=['nFlowLink'],
                                       attrs=dict(units="m",
                                                  standard_name="projection_y_coordinate" ,
                                                  long_name="Center coordinate of net link (velocity point)." ,
                                                  grid_mapping="projected_coordinate_system"))

        ds['FlowElemDomain']=xr.DataArray(np.zeros(len(self.elements),'i2'),dims=['nFlowElem'],
                                          attrs=dict(long_name="Domain number of flow element"))

        ds['FlowLinkDomain']=xr.DataArray(np.zeros(len(links),'i2'),dims=['nFlowLink'],
                                          attrs=dict(long_name="Domain number of flow link"))
        ds['FlowElemGlobalNr']=xr.DataArray(1+np.arange(len(self.elements)),
                                            dims=['nFlowElem'],
                                            attrs=dict(long_name="Global flow element numbering"))

        # node stuff - more of a pain....
        # awkward python2/3 compat.
        xy_to_node=defaultdict(lambda c=count(): next(c) ) # tuple of (x,y) to node
        nodes=np.zeros( ds.FlowElemContour_x.shape, 'i4')
        for c in range(nodes.shape[0]):
            for cc in range(nodes.shape[1]):
                if np.isfinite(packed[c,cc,0]):
                    nodes[c,cc] = xy_to_node[ (packed[c,cc,0],packed[c,cc,1]) ]
                else:
                    nodes[c,cc]=-1
        Nnodes=1+nodes.max()
        node_xy=np.zeros( (Nnodes,2), 'f8')
        for k,v in iteritems(xy_to_node):
            node_xy[v,:]=k

        ds['Node_x']=xr.DataArray(node_xy[:,0],dims=['nNode'])
        ds['Node_y']=xr.DataArray(node_xy[:,1],dims=['nNode'])

        ds['FlowElemContour_node']=xr.DataArray(nodes,dims=['nFlowElem','nFlowElemContourPts'],
                                                attrs=dict(cf_role="face_node_connectivity",
                                                           long_name="Maps faces to constituent vertices/nodes",
                                                           start_index=0))
        # Edges
        points=np.array( [ds.Node_x.values,
                          ds.Node_y.values] ).T
        cells=np.array(ds.FlowElemContour_node.values)
        ug=unstructured_grid.UnstructuredGrid(points=points,
                                              cells=cells)
        ug.make_edges_from_cells()
        
        # Note that this isn't going to follow any particular ordering
        ds['FlowEdge_node']=xr.DataArray(ug.edges['nodes'],dims=['nFlowEdge','nEdgePts'],
                                         attrs=dict(cf_role="edge_node_connectivity",
                                                    long_name = "Maps edge to constituent vertices" ,
                                                    start_index=0))

        # from sundwaq - for now assume that we have a 
        sub=self.open_flowgeom(0)

        ds['nFlowMesh_layers']=xr.DataArray(sub.nFlowMesh_layers[:],
                                            dims=['nFlowMesh_layers'],
                                            attrs=dict(standard_name="ocean_zlevel_coordinate",
                                                       long_name="elevation at layer midpoints" ,
                                                       positive="up" ,
                                                       units="m" ,
                                                       bounds="nFlowMesh_layers_bnds"))
        # maybe some kind of xarray bug?  the data array appears fine, but when it
        # makes it to the dataset, it's all nan.
        # changing the name of the ending variable makes no difference
        # changing the name of the dimension does fix it.
        # work around is to give it a different dimension name.  kludge. FIX.
        #ds['nFlowMesh_layers_bnds']=xr.DataArray(sub.nFlowMesh_layers_bnds[:].copy(),
        #                                         dims=['nFlowMesh_layers2','d2'])

        # this syntax works better:
        ds['nFlowMesh_layers_bnds']=( ['nFlowMesh_layers','d2'],
                                      sub.nFlowMesh_layers_bnds[:].copy() )

        ds['FlowMesh']=xr.DataArray(1,
                                    attrs=dict(cf_role = "mesh_topology" ,
                                               long_name = "Topology data of 2D unstructured mesh" ,
                                               dimension = 2 ,
                                               node_coordinates = "Node_x Node_y" ,
                                               face_node_connectivity = "FlowElemContour_node" ,
                                               edge_node_connectivity = "FlowEdge_node" ,
                                               face_coordinates = "FlowElem_xcc FlowElem_ycc" ,
                                               face_face_connectivity = "FlowLink"))

        # global attrs
        ds.attrs['institution'] = "San Francisco Estuary Institute"
        ds.attrs['references'] = "http://www.deltares.nl" 
        ds.attrs['source'] = "Python/Delft tools, rustyh@sfei.org" 
        ds.attrs['history'] = "Converted from SUNTANS run" 
        ds.attrs['Conventions'] = "CF-1.5:Deltares-0.1" 
        return ds

class HydroAggregator(DwaqAggregator):
    """ Aggregate hydro, where the source hydro is already in one hydro
    object.
    """
    def __init__(self,hydro_in,**kwargs):
        self.hydro_in=hydro_in
        super(HydroAggregator,self).__init__(nprocs=1,
                                             **kwargs)

    def open_hyd(self,p,force=False):
        assert p==0
        return self.hydro_in

    def grid(self):
        if self.agg_shp is not None:
            g=unstructured_grid.UnstructuredGrid.from_shp(self.agg_shp)
            self.log.info("Inferring grid from aggregation shapefile")
            #NB: the edges here do *not* line up with links of the hydro.
            # at some point it may be possible to adjust the aggregation
            # shapefile to make these line up.
            g.make_edges_from_cells()
            return g
        else:
            return self.hydro_in.grid()
    
    def infer_nprocs(self):
        # return 1 # should never get called.
        assert False

    def group_boundary_elements(self):
        # in the simple case with 1:1 mapping, we can just delegate
        # to the hydro_in.
        if self.agg_shp is None: # tantamount to 1:1 mapping
            return self.hydro_in.group_boundary_elements()
        else:
            assert False # try using group_boundary_links() instead!

    def group_boundary_links(self):
        self.hydro_in.infer_2d_links()
        self.infer_2d_links()

        unagg_lgroups = self.hydro_in.group_boundary_links()

        # initialize 
        bc_lgroups=np.zeros(self.n_2d_links,self.link_group_dtype)
        bc_lgroups['id']=-1 # most links are internal and not part of a boundary group
        for lg in bc_lgroups:
            lg['attrs']={} # we have no add'l information for the groups.

        sel_bc=np.nonzero( (self.links[:,0]<0) )[0]

        for group_id,bci in enumerate(sel_bc):
            unagg_matches=np.nonzero(self.link_global_to_agg==bci)[0]
            m_groups=unagg_lgroups[unagg_matches]
            for extra in m_groups[1:]:
                # this means that multiple unaggregated link groups map to the
                # same aggregated link.  So we need some application-specific way
                # of combining them.
                # absent that, the first match will be carried through
                if extra['name'] != m_groups[0]['name']:
                    self.log.warning('Not ready for aggregating boundary link groups - skipping %s'%extra['name'])
            bc_lgroups['id'][bci] = group_id
            if len(m_groups):
                bc_lgroups['name'][bci] = m_groups['name'][0]
                bc_lgroups['attrs'][bci] = m_groups['attrs'][0]
            else:
                self.log.warning("Nobody matched to this aggregated boundary link group bci=%d"%bci)
                bc_lgroups['name'][bci] = "group_%d"%group_id
                bc_lgroups['attrs'][bci] = {}
                break
        return bc_lgroups

    n_2d_links=None
    exch_to_2d_link=None
    links=None
    def infer_2d_links(self): # DwaqAggregator version
        """
        populate self.n_2d_links, self.exch_to_2d_link, self.links 
        note: compared to the incoming _grid_, this may include internal
        boundary exchanges.
        exchanges are identified based on unique from/to pairs of 2d elements.
        in the aggregated case, can additionally distinguish based on the
        collection of unaggregated exchanges which map to these.
        """

        if self.exch_to_2d_link is None:
            self.infer_2d_elements() 
            poi0=self.pointers-1

            # map 0-based exchange index to 0-based link index, limited
            # to horizontal exchangse
            exch_to_2d_link=np.zeros(self.n_exch_x+self.n_exch_y,[('link','i4'),
                                                                  ('sgn','i4')])
            exch_to_2d_link['link']=-1

            #  track some info about links
            links=[] # elt_from,elt_to
            mapped=dict() # (src_2d, dest_2d) => link idx

            # two boundary exchanges, can't be distinguished based on the internal segment.
            # but we can see which unaggregated exchanges/links map to them. 
            # at this point, is there ever a time that we don't want to keep these separate?
            # I think we always want to keep them separate, the crux is how to keep track of
            # who is who between layers.  And that is where we can use the mapping from the
            # unaggregated hydro, where the external id, instead of setting it to -1 and
            # distinguishing only on aggregated internal segment, we can now refine that
            # and label it based on ... maybe the smallest internal segment for boundary
            # exchanges which map to this aggregated exchange?

            self.hydro_in.infer_2d_links()
            # some of the code below can't deal with multiple subdomains
            assert self.exch_local_to_agg.shape[0]==1

            # and special to aggregated code, also build up a mapping of unaggregated
            # links to aggregated links.  And since we're forcing this code to deal with
            # only a single, global unaggregated domain, this mapping is just global to agg.
            # Maybe this should move out to a more general purpose location??
            link_global_to_agg=np.zeros(self.hydro_in.n_2d_links,'i4')-1

            for exch_i,(a,b,_,_) in enumerate(poi0[:self.n_exch_x+self.n_exch_y]):
                # probably have to speed this up with some hashing
                my_unagg_exchs=np.nonzero(self.exch_local_to_agg[0]==exch_i)[0]
                # this is [ (link, sgn), ... ]
                my_unagg_links=self.hydro_in.exch_to_2d_link[my_unagg_exchs]
                if a>=0:
                    a2d=self.seg_to_2d_element[a]
                else:
                    # assuming this only works for global domains
                    # we *could* have multiple unaggregated boundary exchanges mapping
                    # onto this single aggregated boundary exchange.  or not.
                    # what do we know about how the collection of unagg links will be
                    # consistent across layers? ... hmmmph
                    # unsure.. but will use the smallest unaggregated link as a label
                    # to make this aggregated link distinction
                    a2d=-1 - my_unagg_links['link'].min()

                assert b>=0 # too lazy, and this shouldn't happen. 
                b2d=self.seg_to_2d_element[b]

                k='not yet set'
                if (b2d,a2d) in mapped:
                    k=(b2d,a2d) 
                    exch_to_2d_link['link'][exch_i] = mapped[k]
                    exch_to_2d_link['sgn'][exch_i]=-1
                else:
                    k=(a2d,b2d)
                    if k not in mapped:
                        mapped[k]=len(links)
                        # does anyone use the values in links[:,0] ??
                        links.append( [a2d,b2d] )

                    exch_to_2d_link['link'][exch_i] = mapped[k]
                    exch_to_2d_link['sgn'][exch_i]=1
                # record this mapping for later use.  There is some duplicated
                # effort here, since in most cases we'll get the same answer for each
                # of the exchanges in this one link.  But it's possible that some
                # exchanges exist at only certain elevations, or something?  for now
                # duplicate effort in exchange for being sure that all of the links
                # get set.
                # actually, getting some cases where this gets overwritten with
                # different values.  Shouldn't happen!
                prev_values=link_global_to_agg[my_unagg_links['link']]
                # expect that these are either already set, or uninitialized.  but if
                # set to a different link, then we have problems.
                prev_is_okay= (prev_values==mapped[k]) | (prev_values==-1)
                assert np.all(prev_is_okay)
                link_global_to_agg[my_unagg_links['link']]=mapped[k]

            self.link_global_to_agg=link_global_to_agg
            links=np.array(links)
            n_2d_links=len(links)

            ##

            # Bit of a sanity warning on multiple boundary exchanges involving the
            # same segment - this would indicate that there should be multiple 2D
            # links into that segment, but this generic code doesn't have a robust
            # way to deal with that.
            if 1:
                # get 172 of these now.  sounds roughly correct.
                # ~50 in the ocean, 113 or 117 sources, and a handful of
                # others (false_*) which take up multiple links for
                # a single source.

                # indexes of which links are boundary
                bc_links=np.nonzero( links[:,0] < 0 )[0]

                for bc_link in bc_links:
                    # index of which exchanges map to this link
                    exchs=np.nonzero( exch_to_2d_link['link']==bc_link )[0]
                    # link id, sgn for each of those exchanges
                    ab=exch_to_2d_link[exchs]
                    # find the internal segments for each of those exchanges
                    segs=np.zeros(len(ab),'i4')
                    sel0=exch_to_2d_link['sgn'][exchs]>0 # regular order
                    segs[sel0]=poi0[exchs,1]
                    if np.any(~sel0):
                        # including checking for weirdness
                        self.log.warning("Some exchanges had to be flipped when flattening to 2D links")
                        segs[~sel0]=poi0[exchs,0]
                    # And finally, are there any duplicates into the same segment? i.e. a segment
                    # which has multiple boundary exchanges which we have failed to distinguish (since
                    # in this generic implementation we have little info for distinguishing them).
                    # note that in the case of suntans output, this is possible, but if it has been
                    # mapped from multiple domains to a global domain, those exchanges have probably
                    # already been combined.
                    if len(np.unique(segs)) < len(segs):
                        self.log.warning("In flattening exchanges to links, link %d has ambiguous multiple exchanges for the same segment"%bc_link)

            ##
            self.exch_to_2d_link=exch_to_2d_link
            self.links=links
            self.n_2d_links=n_2d_links

    def plot_aggregation(self,ax=None):
        """ 
        schematic of the original grid, aggregated grid, links
        """
        gagg=self.grid()
        gun=self.hydro_in.grid()

        if ax is None:
            ax=plt.gca()

        coll_agg=gagg.plot_cells(ax=ax)
        coll_agg.set_facecolor('none')

        coll_gun=gun.plot_edges(ax=ax,lw=0.3)
        ax.axis('equal')

        centers=np.array( [np.array(gagg.cell_polygon(c).centroid)
                           for c in range(gagg.Ncells()) ] )


        ax.plot(centers[:,0],centers[:,1],'go')

        #for elt in range(gagg.Ncells()):
        #    ax.text(centers[elt,0],centers[elt,1],"cell %d"%elt,size=7,color='red')

        for li,(a,b) in enumerate(self.links):
            # find a point representative of the unaggregated links making up this
            # boundary.
            unagg_links=np.nonzero(self.link_global_to_agg==li)
            unagg_links_xs=[]
            for ab in self.hydro_in.links[unagg_links]: # from elt,to elt
                ab=ab[ab>=0]
                unagg_links_xs.append( np.mean(gun.cells_center()[ab],axis=0) )
            edge_x=np.mean(unagg_links_xs,axis=0) 

            pnts=[]
            if a>=0:
                pnts.append( centers[a] )
            pnts.append(edge_x)
            if b>=0:
                pnts.append( centers[b])

            pnts=np.array(pnts)

            ax.plot( pnts[:,0],pnts[:,1],'g-')
            # ec=centers[[a,b]].mean(axis=0)
            # ax.text(ec[0],ec[1],"link %d"%li,size=7)

        
class HydroMultiAggregator(DwaqAggregator):
    """ Aggregate hydro runs with multiple inputs (i.e. mpi hydro run)
    """
    def __init__(self,run_prefix,path,agg_shp=None,nprocs=None,skip_load_basic=False,
                 **kwargs):
        """ 
        run_prefix: maybe 'sun' - it's part of the names of the per-processor directories.
        path: path to the directory containing the per-processor directories
        """
        self.run_prefix=run_prefix
        self.path=path
        super(HydroMultiAggregator,self).__init__(agg_shp=agg_shp,nprocs=nprocs,
                                                  skip_load_basic=skip_load_basic,
                                                  **kwargs)

    def sub_dir(self,p):
        return os.path.join(self.path,"DFM_DELWAQ_%s_%04d"%(self.run_prefix,p))

    _hyds=None
    def open_hyd(self,p,force=False):
        if self._hyds is None:
            self._hyds={}
        if force or (p not in self._hyds):
            self._hyds[p]=HydroFiles(os.path.join(self.sub_dir(p),
                                                  "%s_%04d.hyd"%(self.run_prefix,p)))
        return self._hyds[p]

    def infer_nprocs(self):
        max_nprocs=1024
        for p in range(1+max_nprocs):
            if not os.path.exists(self.sub_dir(p)):
                assert p>0
                return p
        else:
            raise Exception("Really - there are more than %d subdomains?"%max_nprocs)



class HydroStructured(Hydro):
    n_x=n_y=n_z=None # number of cells in three directions

    def __init__(self,**kws):
        """ 
        expects self.n_{x,y,z} to be defined
        """
        super(HydroStructured,self).__init__(**kws)

        # map 3D index to segment index.  1-based
        linear=1+np.arange( self.n_x*self.n_y*self.n_z )
        self.seg_ids=linear.reshape( [self.n_x,self.n_y,self.n_z] )
        
    @property
    def n_seg(self):
        """ active segments """
        return np.sum(self.seg_ids>0)

    @property
    def n_exch_x(self):
        return (self.n_x-1)*self.n_y*self.n_z 
    @property
    def n_exch_y(self):
        return (self.n_y-1)*self.n_x*self.n_z
    @property
    def n_exch_z(self):
        return (self.n_z-1)*self.n_x*self.n_y

    # assumes fully dense grid, and more than 1 z level.
    # @property
    # def n_top(self): # number of surface cells - come first, I think
    #     return self.n_x * self.n_y
    # @property
    # def n_middle(self): # number of mid-watercolumn cells.
    #     return self.n_x * self.n_y*(self.n_z-2)
    # @property
    # def n_bottom(self): # number of bottom cells
    #     return self.n_x * self.n_y

    @property
    def pointers(self):
        pointers=np.zeros( (self.n_exch,4),'i4')
        pointers[...] = self.CLOSED

        # with 3D structured, this will be expanded, and from
        # the seg_ids array it can be auto-generated for sparse
        # grids, too.

        ei=0 # exchange index

        xi=0 ; yi=0 # common index
        for zi in np.arange(self.n_z-1):

            s_up=self.seg_ids[xi,yi,zi]
            s_down=self.seg_ids[xi,yi,zi+1]

            if zi==0: # surface cell - 
                s_upup=self.CLOSED
            else:
                s_upup=self.seg_ids[xi,yi,zi-1]

            if zi==self.n_z-2:
                s_downdown=self.CLOSED
            else:
                s_downdown=self.seg_ids[xi,yi,zi+2]

            pointers[ei,:]=[s_up,s_down,s_upup,s_downdown]
            ei+=1
        return pointers

class FilterHydroBC(Hydro):
    """ 
    Wrapper around a Hydro instance, shift tidal fluxes into changing volumes, with
    only subtidal fluxes.
    not a Hydro subclass to make it easier to forward attributes on to the underlying
    Hydro instance.

    Actually - will switch it back to a subclass, and just forward 
    attributes as needed
    """
    def __init__(self,original,lp_secs=86400*36./24,selection='boundary'):
        """
        selection: which exchanges will be filtered.  
           'boundary': only open boundaries
           'all': all exchanges
           bool array: length Nexchanges, with True meaning it will get filtered.
           int array:  indices of exchanges to be filtered.
        """
        super(FilterHydroBC,self).__init__()
        self.orig=original
        self.selection=selection
        self.lp_secs=float(lp_secs)
        self.apply_filter()

    # awkward handling of scenario - it gets set by the scenario, so we have
    # relay the setattr on to the original hydro
    @property
    def scenario(self):
        return self.orig.scenario
    @scenario.setter
    def scenario(self,value):
        self.orig.scenario=value

    # somewhat manual forwarding of attributes and methods
    n_exch_x=forwardTo('orig','n_exch_x')
    n_exch_y=forwardTo('orig','n_exch_y')
    n_exch_z=forwardTo('orig','n_exch_z')
    pointers  =forwardTo('orig','pointers')
    time0     =forwardTo('orig','time0')
    t_secs    =forwardTo('orig','t_secs')
    seg_attrs =forwardTo('orig','seg_attrs')
    
    boundary_values=forwardTo('orig','boundary_values')
    seg_func       =forwardTo('orig','seg_func')
    bottom_depths  =forwardTo('orig','bottom_depths')
    vert_diffs     =forwardTo('orig','vert_diffs')
    n_seg        =forwardTo('orig','n_seg')
    boundary_defs  =forwardTo('orig','boundary_defs')
    exchange_lengths=forwardTo('orig','exchange_lengths')
    elements       =forwardTo('orig','elements')
    write_geom = forwardTo('orig','write_geom')
    grid = forwardTo('orig','grid')

    group_boundary_links = forwardTo('orig','group_boundary_links')
    group_boundary_element = forwardTo('orig','group_boundary_elements')

    seg_active = forwardTo('orig','seg_active')
    
    def apply_filter(self):
        self.filt_volumes=np.array( [self.orig.volumes(t) for t in self.orig.t_secs] )
        self.filt_flows  =np.array( [self.orig.flows(t)   for t in self.orig.t_secs] )
        self.filt_areas  =np.array( [self.orig.areas(t)   for t in self.orig.t_secs] )
        self.orig_volumes=self.filt_volumes.copy()
        self.orig_flows  =self.filt_flows.copy()
        
        dt=np.median(np.diff(self.t_secs))
        pointers=self.pointers

        # 4th order butterworth gives better rejection of tidal
        # signal than FIR filter.
        # but there can be some transients at the beginning, so pad the flows
        # out with 0s:
        npad=int(5*self.lp_secs / dt)
        pad =np.zeros(npad)
        
        for j in self.exchanges_to_filter():
            # j: index into self.pointers.  
            segA,segB=pointers[j,:2]

            flow_padded=np.concatenate( ( pad, 
                                          self.filt_flows[:,j],
                                          pad) )
            lp_flows=filters.lowpass(flow_padded,
                                       cutoff=self.lp_secs,dt=dt)
            lp_flows=lp_flows[npad:-npad] # trim the pad
            
            # separate into tidal and subtidal constituents
            tidal_flows=self.filt_flows[:,j]-lp_flows
            self.filt_flows[:,j]=lp_flows 

            tidal_volumes= np.cumsum(tidal_flows[:-1]*np.diff(self.t_secs))
            tidal_volumes= np.concatenate ( ( [0],
                                              tidal_volumes ) )
            # a positive flow is *out* of segA, and *in* to segB
            # positive volumes represent water which is now part of the cell
            if segA>0:
                self.filt_volumes[:,segA-1] += tidal_volumes
                #if np.any( self.filt_volumes[:,segA-1]<0 ):
                #    self.log.warning("while filtering fluxes had negative volume (may be temporary)")
            if segB>0:
                self.filt_volumes[:,segB-1] -= tidal_volumes
                #if np.any( self.filt_volumes[:,segB-1]<0 ):
                #    self.log.warning("while filtering fluxes had negative volume (may be temporary)")

        self.adjust_negative_volumes()

        # it's possible to have some transient negative volumes that work themselves out
        # when other fluxes are included.  but in the end, can't have any negatives.
        assert( np.all(self.filt_volumes>=0) )

        if np.any(self.filt_volumes<self.min_volume):
            self.log.warning("All volumes non-negative, but some below threshold of %f"%self.min_volume)

        self.adjust_plan_areas()

    min_volume=0.0 # 
    min_area = 1.0 # this is very important, I thought
    # actually, well, it may be that planform_areas must be positive, but exchange
    # areas can be zero.  Since those are closely linked, it's easiest and doesn't 
    # seem to break anything to enforce a min_area here.
    def adjust_negative_volumes(self):
        has_negative=np.nonzero( np.any(self.filt_volumes<self.min_volume,axis=0 ) )[0]
        dt=np.median(np.diff(self.t_secs))

        for seg in has_negative:
            self.log.info("Attempting to undo negative volumes in seg %d"%seg)
            # Find a BC segment
            bc_exchs=np.nonzero( (self.pointers[:,0] < 0) & (self.pointers[:,1]==seg+1))[0]
            if len(bc_exchs)==0:
                # will lead to a failure 
                self.log.warning("Segment with negative volume has no boundary exchanges")
                continue
            elif len(bc_exchs)>1:
                self.log.info("Segment with negative volume has multiple BC exchanges.  Choosing the first")
            bc_exch=bc_exchs[0]

            orig_vol=self.orig_volumes[:,seg]# here
            orig_flow=self.orig_flows[:,bc_exch]
            filt_vol=self.filt_volumes[:,seg]
            filt_flow=self.filt_flows[:,bc_exch]

            # correct up to min_volume
            err_vol=filt_vol.clip(-np.inf,self.min_volume) - self.min_volume
            corr_flow=-np.diff(err_vol) / np.diff(self.t_secs)
            # last flow entry isn't used, and we don't have the next volume to know 
            # what it should be anyway.
            corr_flow=np.concatenate( ( corr_flow, [0]) )

            orig_rms=utils.rms(orig_flow)
            filt_rms=utils.rms(filt_flow)

            self.filt_volumes[:,seg]   -=err_vol
            self.filt_flows[:,bc_exch]+=corr_flow

            # report change in rms flow:
            upd_rms=utils.rms(self.filt_flows[:,bc_exch])

            print("    Original flow rms: ",orig_rms)
            print("    Filtered flow rms: ",filt_rms)
            print("    Updated flow rms:  ",upd_rms)

    def adjust_plan_areas(self):
        """ 
        Modifying the volume of segments should be reflected in a change
        in at least some exchange area.  Most appropriate is to change
        the planform area.  
        It's not clear what invariants are required, expected, or most common.
        Assume that it's best to keep planform area constant within a water
        column (i.e. a prismatic grid).  Adjusted area is then Aorig*Vfilter/Vorig.
        """

        # for the vertical integration, have to figure out the structure of the
        # water columns.  This populates seg_to_2d_element:
        self.orig.infer_2d_elements()

        # Then sum volume in each water column - ratio of new volume to old volume
        # gives the factor by which plan-areas should be increased.
        # group in the sense of SQL group by
        groups=self.orig.seg_to_2d_element

        volumes     =np.array( [self.volumes(t)      for t in self.t_secs] )
        orig_volumes=np.array( [self.orig.volumes(t) for t in self.t_secs] )

        # sum volume in each water column
        # might have dense output of z-levels, for which there segments which don't
        # belong to a water column - bincount doesn't like those negative values.
        valid=groups>=0
        
        Vratio_filt_to_orig_2d=[ np.bincount(groups[valid],volumes[ti,valid])  / \
                                 np.bincount(groups[valid],orig_volumes[ti,valid])                                 
                                 for ti in range(len(self.t_secs)) ]
        Afactor_per_2d_element=np.array( Vratio_filt_to_orig_2d )

        # loop over vertical exchanges, updating areas
        exchs=self.pointers
        for j in range(self.n_exch_x+self.n_exch_y,self.n_exch):
            segA,segB=exchs[j,:2] - 1 # seg now 0-based
            if segA<0: # boundary
                group=groups[segB]
            elif segB<0: # boundary
                group=groups[segA]
            else:
                assert(groups[segA]==groups[segB])
                group=groups[segA]
            # update this exchanges area, for all time steps
            self.filt_areas[:,j] *= Afactor_per_2d_element[:,group]

        # clean up a slightly different issue while we're at it.
        # since upper layers can dry out, it's possible that we'll
        # add some lowpass flow, but the area will be zero.
        # there is also the very likely case that unused exchanges
        # have zero flow and zero area, but maybe that's not a big deal.
        for exch in range(self.n_exch):
            sel=(self.filt_areas[:,exch]==0) & (self.filt_flows[:,exch]!=0)
            if np.any(sel):
                self.log.warning("Cleaning up zero area exchange %d"%exch)
                if np.all( self.filt_areas[:,exch]==0  ):
                    raise Exception("An exchange has some flow, but never has any area")
                self.filt_areas[sel,exch] = np.nan
                self.filt_areas[:,exch] = utils.fill_invalid(self.filt_areas[:,exch])
                
        # and finally, delwaq2 doesn't like to have any zero-area exchanges, even if
        # they never have any flow.  so they all get unit area.
        
        self.filt_areas[ self.filt_areas<self.min_area ] = self.min_area


    def exchanges_to_filter(self):
        """
        return indices into self.pointers which should get the filtering
        not restricted to boundary exchanges
        """
        # defaults to boundary exchanges
        pointers=self.pointers
        selection=self.selection
        if isinstance(selection,str):
            if selection=='boundary':
                sel=np.nonzero(pointers[:,0]<0)[0]
            elif selection=='all':
                sel=np.arange(len(pointers))
            else:
                assert False
        else:
            selection=np.asarray(selection)
            if selection.dtype==np.bool8:
                sel=np.nonzero(selection)
            else:
                sel=selection
        return sel

    def volumes(self,t):
        ti=self.time_to_index(t)
        return self.filt_volumes[ti,:]
    def flows(self,t):
        ti=self.time_to_index(t)
        return self.filt_flows[ti,:]
    def areas(self,t):
        ti=self.time_to_index(t)
        return self.filt_areas[ti,:]

    def planform_areas(self):
        """ Here have to take into account the time-variability of
        planform area.
        """
        # pull areas from exchange areas of vertical exchanges
    
        seg_z_exch=self.seg_to_exch_z(preference='upper')
    
        # then pull exchange area for each time step
        A=np.zeros( (len(self.t_secs),self.n_seg) )
        for ti,t_sec in enumerate(self.t_secs):
            areas=self.areas(t_sec)
            A[ti,:] = areas[seg_z_exch]

        A[ A<1.0 ] = 1.0 # just to be safe, in case area_min above is removed.
        return ParameterSpatioTemporal(times=self.t_secs,values=A,hydro=self)
    
    def depths(self):
        """ Compute time-varying segment thicknesses.  With z-levels, this is
        a little more nuanced than the standard calc. in delwaq.
        """
        if 1: 
            # just defer depth to the unfiltered data - since we'd actually like
            # to be replicating depth variation, and the filtering is just
            # to reduce numerical diffusion.
            return self.orig.depths()
        else:
            # reconstruct segment thickness from area/volume.
            # use upper, since some bed segment would have a zero area for the
            # lower exchange
            print("Call to depths parameter!")

            seg_z_exch=self.seg_to_exch_z(preference='upper')
            D=np.zeros( (len(self.t_secs),self.n_seg) )
            for ti,t_sec in enumerate(self.t_secs):
                areas=self.areas(t_sec)
                volumes=self.volumes(t_sec)
                D[ti,:] = volumes / areas[seg_z_exch]

            # following new code from WaqAggregator above.
            sel=(~np.isfinite(D))
            D[sel]=0.0

            return ParameterSpatioTemporal(times=self.t_secs,values=D,hydro=self)

    # These cannot be forwarded, b/c other code assumes that after calling,
    # additional state is set on self
    def infer_2d_links(self):
        self.orig.infer_2d_links()
        self.n_2d_links=self.orig.n_2d_links
        self.exch_to_2d_link=self.orig.exch_to_2d_link
        self.links=self.orig.links 


class FilterAll(FilterHydroBC):
    """ Minor specialization when you want filter everything - i.e. turn a tidal
    run into a subtidal run.
    """
    
    def __init__(self,orig):
        super(FilterAll,self).__init__(orig,selection='all')

    def adjust_plan_areas(self):
        """ 
        The original FilterHydroBC code adjust plan areas, 
        meant to shift tidal signals into horizontally expanding/contracting
        segments.
        But when filtering all exchanges, probably better to just remove 
        tidal variation from horizontal exchanges.  
        """
        dt=np.median(np.diff(self.t_secs))

        npad=int(5*self.lp_secs / dt)
        pad =np.zeros(npad)
        
        # loop over horizontal exchanges, updating areas
        poi0=self.pointers-1
        for j in range(self.n_exch_x+self.n_exch_y):
            padded=np.concatenate( ( pad, 
                                     self.filt_areas[:,j],
                                     pad) )
            lp_areas=filters.lowpass(padded,cutoff=self.lp_secs,dt=dt)
            lp_areas=lp_areas[npad:-npad] # trim the pad
            
            self.filt_areas[:,j] = lp_areas

        # FilterHydroBC does some extra work right here, but I'm hoping that's
        # not necessary??
                
        # and finally, delwaq2 doesn't like to have any zero-area exchanges, even if
        # they never have any flow.  so they all get unit area.
        self.filt_areas[ self.filt_areas<self.min_area ] = self.min_area

    def planform_areas(self):
        """ Skip FilterHydroBC's filtering of planform areas, use the original hydro
        instead.
        """
        return self.orig.planform_areas()
    
    def depths(self):
        """ Compute time-varying segment thicknesses.  With z-levels, this is
        a little more nuanced than the standard calc. in delwaq.
        """
        # reconstruct segment thickness from area/volume.
        # use upper, since some bed segment would have a zero area for the
        # lower exchange
        print("Call to depths parameter!")

        # this also gets simpler due to the constant area in each water
        # column.
        seg_z_exch=self.seg_to_exch_z(preference='upper')
        
        D=np.zeros( (len(self.t_secs),self.n_seg) )
        for ti,t_sec in enumerate(self.t_secs):
            areas=self.areas(t_sec)
            volumes=self.volumes(t_sec)
            D[ti,:] = volumes / areas[seg_z_exch]

        # following new code from WaqAggregator above.
        sel=(~np.isfinite(D))
        D[sel]=0.0

        return ParameterSpatioTemporal(times=self.t_secs,values=D,hydro=self)

    def add_parameters(self,hyd):
        hyd=super(FilterAll,self).add_parameters(hyd)

        for key,param in iteritems(self.orig.parameters()):
            self.log.info('Original -> filtered parameter %s'%key)
            # overwrite vertdisper
            if key in hyd and key not in ['vertdisper']:
                self.log.info('  parameter already set')
                continue
            elif isinstance(param,ParameterSpatioTemporal):
                self.log.info("  original parameter is spatiotemporal - let's FILTER")
                hyd[key]=param.lowpass(self.lp_secs)
                if key in ['vertdisper']: # force non-negative
                    # a bit dangerous, since there is no guarantee that lowpass made a copy.
                    # but if it didn't make a copy, and all of the source data were
                    # valid, then there should be nothing to clip.
                    hyd[key].values = hyd[key].values.clip(0,np.inf)
                hyd[key].hydro=self
                self.log.info("  FILTERED.")
            elif isinstance(param,ParameterTemporal):
                self.log.warning("  original parameter is temporal - should filter")
                hyd[key]=param # FIX - copy and set hydro, maybe filter, too.
            else:
                self.log.info("  original parameter is not temporal - just copy")
                hyd[key]=param # ideally copy and set hydro
                
        return hyd


# utility for Sigmified
def rediscretize(src_dx,src_y,n_sigma,frac_samples=None,intensive=True):
    """
    Redistribute src_y values from bins of size src_dx to n_sigma
    bins of equal size.  
    Promises that sum(src_y*src_dx) == sum(dest_y*dest_dx), (or without
    the *_dx part if intensive is False).
    """
    
    #seg_sel_z=np.nonzero( self.hydro_z.seg_to_2d_element==elt )[0]
    #seg_v_z=vol_z[seg_sel_z] # that's src_dx
    # seg_scal_z=scalar_z[seg_sel_z] # that's src_y
    src_dx_sum=src_dx.sum()
    if src_dx_sum==0:
        assert np.all(src_y==0.0)
        return np.zeros(n_sigma)
    
    src_dx = src_dx / src_dx_sum # normalize to 1.0 volume for ease
    src_xsum=np.cumsum(src_dx)

    # would like to integrate that, finding s_i = 10 * (Int i/10,(i+1)/10 s df)
    # instead, use cumsum to handle the discrete integral then interp to pull
    # out the individual values

    if intensive:
        src_y_ext = src_y * src_dx
    else:
        src_y_ext = src_y
        
    cumul_mass =np.concatenate( ( [0],
                                  np.cumsum(src_y_ext) ) )
    frac_sum=np.concatenate( ( [0], src_xsum ) )
    if frac_samples is None:
        frac_samples=np.linspace(0,1,n_sigma+1)
        
    dest_y = np.diff(np.interp(frac_samples,
                               frac_sum,cumul_mass) )
    if intensive:
        dest_y *= n_sigma # assumes evenly spread out layers 
    return dest_y

class Sigmified(Hydro):
    def __init__(self,hydro_z,n_sigma=10,**kw):
        super(Sigmified,self).__init__(**kw)
        self.hydro_z=hydro_z
        self.n_sigma=n_sigma
        self.init_exchanges()
        self.init_lengths()
        self.init_2_to_3_maps()

    def init_2_to_3_maps(self):
        self.infer_2d_elements()
        self.hydro_z.infer_2d_elements()

        elt_to_seg_z=[ [] for _ in range(self.n_2d_elements) ]
        
        #for elt in range(self.n_2d_elements):
        #    self.elt_to_seg_z[elt]=np.nonzero( self.hydro_z.seg_to_2d_element==elt )[0]

        # this should be faster
        for seg,elt in enumerate(self.hydro_z.seg_to_2d_element):
            elt_to_seg_z[elt].append(seg)
        self.elt_to_seg_z=[ np.array(segs) for segs in elt_to_seg_z]

        # similar, but for links, and used for both z and sig
        link_to_exch_z=  [ [] for _ in range(self.n_2d_links)]
        link_to_exch_sig=[ [] for _ in range(self.n_2d_links)]

        for exch,link in enumerate(self.hydro_z.exch_to_2d_link['link']):
            link_to_exch_z[link].append(exch)
        for exch,link in enumerate(self.exch_to_2d_link['link']):
            link_to_exch_sig[link].append(exch)
        self.link_to_exch_z=[ np.array(exchs) for exchs in link_to_exch_z ]
        self.link_to_exch_sig=[ np.array(exchs) for exchs in link_to_exch_sig ]

    @property
    def n_seg(self):
        return self.n_sigma * self.hydro_z.n_2d_elements
    
    time0     =forwardTo('hydro_z','time0')
    t_secs    =forwardTo('hydro_z','t_secs')
    group_boundary_links = forwardTo('hydro_z','group_boundary_links')
    group_boundary_element = forwardTo('hydro_z','group_boundary_elements')

    def init_exchanges(self):
        """
        populates pointers, n_2d_links, n_exch_{x,y,z}
        """
        self.hydro_z.infer_2d_links()

        # links are the same for the z-layer and sigma
        self.links=self.hydro_z.links.copy()
        self.n_2d_links = self.hydro_z.n_2d_links

        poi0_z = self.hydro_z.pointers - 1
        # start with all of the internal exchanges, then the top-layer boundary
        # exchanges, then to lower layers.
        # write it first without any notion of boundary fluxes, then fix it up

        poi0_sig=[]
        exch_to_2d_link=[] # build this up as we go
        n_exch_x=0
        n_exch_z=0

        self.infer_2d_elements()

        n_bc=0 # counter for boundary exchanges
        # horizontal exchanges:
        for sig in range(self.n_sigma):
            for link_i,(link_from,link_to) in enumerate(self.links):
                # doesn't distinguish between boundary conditions and internal
                # possible that boundary conditionsa are only in certain layers.
                # In our specific setup, even boundary conditions which are in the hydro
                # only at the surface probably *ought* to be spread across the
                # water column.
                # But - do need to be smarter about how the outside segments are
                # numbered.
                # In particular, link_from is sometimes negative, and probably we're
                # not supposed to just multiply by sig
                if link_from<0:
                    # first one gets a 0-based index of -2, so -1 in real pointers
                    seg_from=-2 - n_bc
                    n_bc+=1
                else:
                    seg_from=link_from + sig*self.n_2d_elements
                seg_to=link_to + sig*self.n_2d_elements
                exchi=len(poi0_sig)
                poi0_sig.append( [seg_from,seg_to,-1,-1] )
                # all forward:
                exch_to_2d_link.append( [link_i,1] )
        self.n_exch_x=len(poi0_sig)
        
        # vertical exchangse
        for sig in range(self.n_sigma-1):
            for elt in range(self.n_2d_elements):
                seg_from=elt + sig*self.n_2d_elements
                seg_to  =elt + (sig+1)*self.n_2d_elements
                exchi=len(poi0_sig)
                poi0_sig.append( [seg_from,seg_to,-1,-1] )

        # not quite ready for vertical boundary exchanges, so make sure they
        # don't exist in the input
        assert self.hydro_z.pointers[(self.hydro_z.n_exch_x+self.hydro_z.n_exch_y):,:2].min()>0
        
        self.n_exch_y=0
        self.n_exch_z=len(poi0_sig) - self.n_exch_x - self.n_exch_y
        self.pointers=np.array(poi0_sig)+1
        exch_to_2d_link=np.array( exch_to_2d_link )
        self.exch_to_2d_link=np.zeros(self.n_exch_x+self.n_exch_y,
                                      [('link','i4'),('sgn','i4')])
        self.exch_to_2d_link['link']=exch_to_2d_link[:,0]
        self.exch_to_2d_link['sgn']=exch_to_2d_link[:,1]

    def init_lengths(self):
        lengths = np.zeros( (self.n_exch,2), 'f4' )
        lengths[:self.n_exch_x+self.n_exch_y,:] = np.tile(self.hydro_z.exchange_lengths[:self.n_2d_links,:],(self.n_sigma,1))
        lengths[self.n_exch_x+self.n_exch_y:,:] = 1./self.n_sigma
        self.exchange_lengths=lengths
        
    def areas(self,t):
        poi0_sig = self.pointers - 1

        Af_sig=np.zeros(len(poi0_sig),'f4')

        # calculate flux-face areas
        Af_z = self.hydro_z.areas(t)
        Af_x_z=Af_z[:self.hydro_z.n_exch_x + self.hydro_z.n_exch_y]

        # horizontal first:
        # sum per 2d link:
        Af_per_link_z = np.bincount( self.hydro_z.exch_to_2d_link['link'],
                                     weights=Af_x_z )
        # then evenly divide by n_sigma
        Af_sig[:self.n_exch_x+self.n_exch_y] = Af_per_link_z[ self.exch_to_2d_link['link'] ] / self.n_sigma

        # vertical: here there could be z-layer water columns with no exchanges, so no area here, but
        # sigma grid will have areas
        # instead, we go to planform_areas()
        plan_areas=self.planform_areas().evaluate(t=t).data # [Nseg]

        from_seg=poi0_sig[self.n_exch_x+self.n_exch_y:,0]
        assert np.all( from_seg>= 0 )
        Af_sig[self.n_exch_x+self.n_exch_y:] = plan_areas[from_seg]
        return Af_sig
        
    def volumes(self,t_sec):
        self.hydro_z.infer_2d_elements()

        v_z = self.hydro_z.volumes(t_sec)

        seg_to_elt = self.hydro_z.seg_to_2d_element.copy() # negative for below-bed segments
        assert np.all( v_z[ seg_to_elt<0 ] ==0 ) # sanity.

        seg_to_elt[ seg_to_elt<0 ] = 0 # to allow easy summation.

        elt_v_z = np.bincount(seg_to_elt,weights=v_z)

        # divide volume evenly across layers, tile out to make dense linear matrix.
        seg_v_sig=np.tile( elt_v_z/self.n_sigma, self.n_sigma)
        return seg_v_sig
    
    dz_deficit_threshold=-0.001
    def flows(self,t):
        Q_z = self.hydro_z.flows(t)
        Q_sig=np.zeros( self.n_exch, 'f8' )
        A_z  =self.hydro_z.areas(t)
        A_sig=self.areas(t)

        frac_samples=np.linspace(0,1,self.n_sigma+1)
        
        # start with just the horizontal flows:
        for link_i,link in enumerate(self.links):
            exch_sel_z  =self.link_to_exch_z[link_i]   # np.nonzero( self.hydro_z.exch_to_2d_link['link']==link_i )[0]
            exch_sel_sig=self.link_to_exch_sig[link_i] # np.nonzero( self.exch_to_2d_link['link']==link_i )[0]

            areas_z=A_z[exch_sel_z]
            areas_sig=A_sig[exch_sel_sig]

            # This hasn't been a problem, and it's slow - so skip it.
            # assert np.allclose( np.sum(areas_z), np.sum(areas_sig) ) # sanity

            # aggregated z-level grid doesn't necessarily have the same sign for all of the
            # exchanges in a column, but sigma does.  Go ahead and flip signs as needed
            # trouble with sgn - exch_sel_z goes up to 4727, while exch_to_2d_link 
            q_z = Q_z[exch_sel_z] * self.hydro_z.exch_to_2d_link['sgn'][exch_sel_z]

            Q_sig[exch_sel_sig] = rediscretize(areas_z,q_z,self.n_sigma,
                                               frac_samples=frac_samples,
                                               intensive=False)

        # Vertical fluxes:
        # starting with volume now, apply the horizontal fluxes to get predicted volumes
        # for the next step.  Step surface to bed, any discrepancy between predicted and
        # the reported Vnext should be vertical flux.  

        poi0=self.pointers-1
        Vnow=self.volumes(t)
        dt=self.t_secs[1] - self.t_secs[0]
        Vnext=self.volumes(t+dt)

        Vpred=Vnow.copy()

        if 0: # reference implementation
            for exch in range(self.n_exch_x):
                seg_from,seg_to = poi0[exch,:2]
                if seg_from>=0:
                    Vpred[seg_from] -= Q_sig[exch]*dt
                    Vpred[seg_to] += Q_sig[exch]*dt
        else: # vectorized implementation of that:
            seg_from=poi0[:self.n_exch_x,0]
            seg_to  =poi0[:self.n_exch_x,1]
            seg_from_valid=(seg_from>=0)

            # This fails because duplicates in seg_from/to overwrite each other.
            # simple vectorization fails due to duplicate indices in seg_from/to
            Vpred -= dt*np.bincount(seg_from[seg_from_valid],
                                    weights=Q_sig[:self.n_exch_x][seg_from_valid],
                                    minlength=len(Vpred))

            Vpred += dt*np.bincount(seg_to,
                                    weights=Q_sig[:self.n_exch_x],
                                    minlength=len(Vpred))

        for exch in range(self.n_exch_x+self.n_exch_y,self.n_exch)[::-1]:
            seg_from,seg_to = poi0[exch,:2]
            if seg_from<0:
                # shouldn't happen, as we're not yet considering vertical boundary exchanges,
                # and besides there shouldn't be adjustments to boundary fluxes, anyway.
                continue
            Vsurplus=Vpred[seg_to] - Vnext[seg_to]
            Q_sig[exch] = -Vsurplus / dt
            Vpred[seg_from] += Vsurplus
            Vpred[seg_to]   -= Vsurplus

        # if 0: # vectorization more complicated here - may return...
        #     exchs=np.arange(self.n_exch_x+self.n_exch_y,self.n_exch)[::-1]
        #     segs_from,segs_to = poi0[exchs,:2]
        #     assert np.all(segs_from>=0)
        #     
        #     Vsurplus=Vpred0[segs_to] - Vnext[segs_to]
        #     Q_sig0[exchs] = -Vsurplus / dt
        # 
        #     for exch in :
        #         Vpred[seg_from] += Vsurplus
        #         Vpred[seg_to]   -= Vsurplus
            
        rel_err = (Vpred - Vnext) / (1+Vnext)
        rel_err = np.abs(rel_err)
        rel_err_thresh=0.5
        if rel_err.max() >= rel_err_thresh:
            self.log.warning("Vertical fluxes still had relative errors up to %.2f%%"%( 100*rel_err.max() ) )
            self.log.warning("  at t=%s  (ti=%d)"%(t, self.time_to_index(t)))

            # It's possible that precip or mass limits from the hydro code yield Vpred which go negative.
            # this would be bad news for dwaq scalar transport.  Find any water columns which have a
            # segment that goes negative, and fully redistribute the verical fluxes to have the new volumes
            # equal throughout the water column.

            # find the segments with a violation:
            bads = np.nonzero( rel_err>= rel_err_thresh )[0]
            bad_elts = np.unique( self.seg_to_2d_element[bads] )
            self.log.warning(" Bad 2d elements: %s"%str(bad_elts))
            for bad_elt in bad_elts:
                segs=np.nonzero( self.seg_to_2d_element==bad_elt )[0]
                # assumption of evenly spaced, no vertical boundaries, etc.
                exchs=self.n_exch_x + self.n_exch_y + bad_elt + np.arange(self.n_sigma-1)*self.n_2d_elements
                assert np.all( self.seg_to_2d_element[poi0[exchs,:2]] == bad_elt ) # sanity check

                # Q_sig as it stands leads to volumes Vpred[segs].
                # we'd like for it to lead to volumes Vpred[segs].mean()
                # 
                netQ_correction = (Vpred[segs] - Vpred[segs].mean()) / dt

                # This seems like the right approach, but leaves Q_sig[exchs]==[0,...]
                # which is suspicious
                # but - this is a single layer in hydro_z, so all horizontal fluxes will be
                # evenly divided across the sigma layers, thus there is no gradient in transport
                # to drive vertical velocities.
                # I think it's correct

                # drop the last one - it's interpretation is a flux out of the bed cell,
                # to some cell below it.  It had better be close to zero...
                Q_sig[exchs] += np.cumsum(netQ_correction)[:-1]

                Vpred[segs] = Vpred[segs].mean()

        Vpred_min=Vpred.min()
        if Vpred_min < 0.0:
            # normalize by area to see just how bad these are:
            plan_areas=self.planform_areas().evaluate(t=t).data # [Nseg]
            dz_pred=Vpred / plan_areas

            # compare to the errors already in hydro_z:
            z_errors=self.check_hydro_z_conservation(ti=self.time_to_index(t))
            
            self.log.warning("Some predicted volumes are negative, for min(dz)=%f at seg %d"%(dz_pred.min(),
                                                                                              np.argmin(dz_pred)))
            # assert dz_pred.min()>self.dz_deficit_threshold # -0.001
            # no need to sum over water column in the dz_pred figures, since it's evenly distributed
            # just multiply by n_sigma.
            # make sure we're not more negative than the size of the errors in hydro_z. Note that
            # this is not quite apples-to-apples - z_errors will be worse since it is the error between
            # predicted thickness and prescribed thickness, while dz_pred is a deficit below 0 volume.
            assert self.n_sigma*dz_pred.min() > z_errors.min()
            self.log.warning(" Apparently the erros in the z-layer model are at least as bad")
        
        return Q_sig

    def check_hydro_z_conservation(self,ti):
        """ For reality checks on flows above, reach back to the z-layer 
        hydro and see if there were already continuity errors.  

        Given a time index into t_secs, perform continuity check on ti => ti+1
        and return the error in the "prediction", per-element in terms of thickness.
        So if the fluxes suggest that a water column went from 0.10m to -0.05m thick,
        but the volume data suggests it just went from 0.10m to 0.01m, then that element
        would get an error of -0.06m.
        """
        hyd=self.hydro_z
        QtodV,QtodVabs=hyd.mats_QtodV()

        t_last=hyd.t_secs[ti]
        t_next=hyd.t_secs[ti+1]
        Qlast=hyd.flows( t_last )
        Vlast=hyd.volumes( t_last )
        Vnow=hyd.volumes( t_next )

        plan_areas=hyd.planform_areas()
        seg_plan_areas=plan_areas.evaluate(t=t_last).data

        dt=t_next - t_last
        dVmag=QtodVabs.dot(np.abs(Qlast)*dt)
        Vpred=Vlast + QtodV.dot(Qlast)*dt

        err=Vpred - Vnow 
        valid=(Vnow+dVmag)!=0.0

        # for the purposes of the sigmify code, want this in water columns:
        hyd.infer_2d_elements() 

        sel=hyd.seg_to_2d_element>=0
        elt_err=np.bincount(hyd.seg_to_2d_element[sel],
                            weights=err[sel]/seg_plan_areas[sel],
                            minlength=hyd.n_2d_elements)
        return elt_err
    
    def segment_interpolator(self,t_sec,scalar_z):
        """ 
        Generic segment scalar aggregation
        t_sec: simulation time, integer seconds
        scalar_z: values from the z grid
        """
        orig_t_sec=t_sec
        t_sec=utils.nearest_val(self.t_secs,orig_t_sec)
        dt=self.t_secs[1] - self.t_secs[0]
        if abs(orig_t_sec-t_sec) > 1.5*dt:
            self.log.warning("segment_interpolator: requested time and my time off by %.2f steps"%( (orig_t_sec-t_sec)/dt ))
        
        self.infer_2d_elements()
        interp_scalars=np.zeros(self.n_seg,'f4')

        vol_z=self.hydro_z.volumes(t_sec)

        elt_to_seg_z=self.elt_to_seg_z
        
        # Start with super-slow approach - looping through elements
        # yes, indeed, it is super slow.
        frac_samples=np.linspace(0,1,self.n_sigma+1)
        for elt in range(self.n_2d_elements):
            # this is going to hurt:
            seg_sel_z=elt_to_seg_z[elt] # np.nonzero( self.hydro_z.seg_to_2d_element==elt )[0]
            seg_v_z=vol_z[seg_sel_z]
            seg_scal_z=scalar_z[seg_sel_z]
            
            seg_scal_sig = rediscretize(seg_v_z,seg_scal_z,self.n_sigma,
                                        intensive=True,
                                        frac_samples=frac_samples)
            
            # stripe it out across the nice evenly spaced layers:
            interp_scalars[elt::self.n_2d_elements]=seg_scal_sig

            #plt.figure(3).clf()
            #plt.plot(frac_sum,s_mass,'k-o')
            #plt.figure(2).clf()
            #plt.bar(left=seg_vsum_z-seg_v_z,width=seg_v_z,height=seg_scal_z)
            #plt.bar(left=frac_samples[:-1],width=1./self.n_sigma,height=seg_scal_sig,
            #        color='g',alpha=0.5)

        return interp_scalars
    def add_parameters(self,hyd):
        for p,param in iteritems(self.hydro_z.parameters(force=False)):
            if p=='surf':
                # copy top value down to others
                hyd[p] = self.param_z_copy_from_surface(param)
            else:
                self.log.info("Adding hydro parameter with z interpolation %s"%p)
                hyd[p] = self.param_z_interpolate(param)
        self.log.info("Done with Hydro::add_parameters()")
        return hyd
    def param_z_interpolate(self,param_z):
        if isinstance(param_z,ParameterSpatioTemporal):
            def interped(t_sec,self=self,param_z=param_z):
                return self.segment_interpolator(t_sec=t_sec,
                                                 scalar_z=param_z.evaluate(t=t_sec).data)
            # this had been using self.t_secs, but for temperature, and probably in general,
            # we should respect the original times
            return ParameterSpatioTemporal(func_t=interped,
                                           times=param_z.times,
                                           hydro=self)
        elif isinstance(param_z,ParameterSpatial):
            # interpolate based on the initial volumes
            seg_sig=self.segment_interpolator(t_sec=self.t_secs[0],
                                              scalar_z=param_z.data)
            return ParameterSpatial(per_segment=seg_sig,hydro=self)
        else:
            return param_z # constant or only time-varying
    def param_z_copy_from_surface(self,param_z):
        # all we know how to deal with so far:
        assert isinstance(param_z,ParameterSpatial)
        per_seg_z=param_z.data
        self.infer_2d_elements()
        per_seg_sig = np.tile( per_seg_z[:self.n_2d_elements], self.n_sigma )
        return ParameterSpatial(per_segment=per_seg_sig,hydro=self)
        
    def planform_areas(self):
        return self.param_z_copy_from_surface(self.hydro_z.planform_areas())
    def infer_2d_elements(self):
        # easy!
        if self.seg_to_2d_element is None:
            self.hydro_z.infer_2d_elements()
            self.n_2d_elements=self.hydro_z.n_2d_elements
            self.seg_to_2d_element = np.tile( np.arange(self.n_2d_elements),
                                              self.n_sigma )
            self.seg_k = np.repeat( np.arange(self.n_sigma), self.n_2d_elements )
        return self.seg_to_2d_element
    def infer_2d_links(self):
        # this is pre-computed in init_exchanges / delegated to
        # hydro_z
        return

    def grid(self):
        return self.hydro_z.grid()

    def get_geom(self):
        # copy all of the 2D info from the z-layer grid, and just slide
        # in appropriate sigma layer info for the vertical
        ds=self.hydro_z.get_geom()

        bounds = np.linspace(0,-1,1+self.n_sigma)
        centers= 0.5*(bounds[:-1] + bounds[1:])

        # remove these first, and xarray forgets about the old size for
        # this dimension allowing it to be redefined
        del ds['nFlowMesh_layers_bnds']
        del ds['nFlowMesh_layers']

        ds['nFlowMesh_layers']=xr.DataArray( centers,
                                             dims=['nFlowMesh_layers'],
                                             attrs=dict(standard_name="ocean_sigma_coordinate",
                                                        long_name="elevation at layer midpoints",
                                                        formula_terms="sigma: nFlowMesh_layers eta: eta depth: FlowElem_bl",
                                                        positive="up" ,
                                                        units="m" ,
                                                        bounds="nFlowMesh_layers_bnds"))
        
        # order correct?
        bounds_d2=np.array( [bounds[:-1], 
                             bounds[1:]] ).T
        # trying this without introducing the duplicate dimensions
        # this syntax avoids issues with trying interpolate between coordinates
        ds['nFlowMesh_layers_bnds']=( ('nFlowMesh_layers','d2'), bounds_d2 )

        if 'nFlowMesh_layers2' in ds:
            ds=ds.drop('nFlowMesh_layers2')

        return ds
    
    #  depths and bottom_depths: I think generic implementations will be sufficient


    
class Substance(object):
    _scenario=None # set by scenario
    active=True
    initial=None

    @property
    def scenario(self):
        return self._scenario
    @scenario.setter
    def scenario(self,s):
        self._scenario=s
        self.initial.scenario=s

    def __init__(self,initial=None,name=None,scenario=None,active=True):
        self.name = name or "unnamed"
        self.initial=initial or Initial(default=0.0)
        self.scenario=scenario
        self.active=active
        self.initial.scenario=scenario

    def lookup(self):
        if self.scenario:
            return self.scenario.lookup_item(self.name)
        else:
            return None

    def copy(self):
        # assumes that name and scenario will be set by the caller separately
        return Substance(initial=self.initial,active=self.active)

class Initial(object):
    """ 
    descriptions of initial conditions.
    Initial() # default to 0
    Initial(10) # default to 10
    Initial(seg_values=[Nseg values]) # specify spatially varying directly
    ic=Initial()
    ic[1] = 12 # default to 0, segment 12 (0-based) gets 12.

    for this last syntax, 1 and 12 just have to work for numpy array assignment.
    """
    scenario=None # Substance will set this.
    def __init__(self,default=0.0,seg_values=None):
        assert np.isscalar(default)
        self.default=default
        self.seg_values=seg_values

    def __setitem__(self,k,v):
        # print "Setting initial condition for segments",k,v
        if self.seg_values is None:
            self.seg_values = np.zeros(self.scenario.hydro.n_seg,'f4')
            self.seg_values[:] = self.default

        self.seg_values[k]=v

    # def eval_for_segment(self,seg_idx):
    #     if self.segment is not None:
    #         return self.segment[seg_idx]
    #     else:
    #         return self.d

    # just needs to have seg_values populated before the output is generated.
    # what is the soonest that we know about hydro geometry?
    # the substances know the scenario, but that's before 


class ModelForcing(object):
    """ 
    holds some common code between BoundaryCondition and 
    Load.
    """
    scenario=None # set by the scenario

    def __init__(self,items,substances,data):
        if isinstance(substances,str):
            substances=[substances]
        if isinstance(items,str) or not isinstance(items,Iterable):
            items=[items]
        self.items=items
        self.substances=substances
        self.data=data

    def text_item(self):
        lines=['ITEM']

        for item in self.items:
            item=self.fmt_item(item)
            lines.append("  '%s'"%item )
        return "\n".join(lines)

    def text_substances(self):
        lines=['CONCENTRATION'] # used to be plural - should be okay this way
        lines.append("   " + "  ".join(["'%s'"%s for s in self.substances]))
        return "\n".join(lines)

    def text_data(self):
        lines=[]

        # FIX: somewhere we should limit the output to the simulation period plus
        # some buffer.
        data=self.data

        if isinstance(data,pd.Series):
            # coerce to tuple with datenums
            data=(utils.to_dnum(data.index.values),
                  data.values)

        if isinstance(data,tuple):
            data_t,data_values=data
            lines.append('TIME {}'.format(self.time_interpolation))
        else:
            data_t=[None]
            data_values=np.asarray(data)

        lines.append('DATA')

        for ti,t in enumerate(data_t):
            if len(data_t)>1: # time varying
                step_data=data_values[ti,...]
                lines.append(self.scenario.fmt_datetime(t)) # like 1990/08/05-12:30:00
            else:
                step_data=data_values

            for item_i,item in enumerate(self.items):
                if step_data.ndim==2:
                    item_data=step_data[item_i,:]
                else:
                    item_data=step_data

                line=[]
                for sub_i,substance in enumerate(self.substances):
                    if item_data.ndim==1:
                        item_sub_data=item_data[sub_i]
                    else:
                        item_sub_data=item_data

                    line.append("%g"%item_sub_data) 
                line.append("; %s"%self.fmt_item(item))
                lines.append(" ".join(line))
        return "\n".join(lines)

    def fmt_item(self,item):
        return str(item) # probably not what you want...

    def text(self):
        lines=[self.text_item(),
               self.text_substances(),
               self.text_data()]
        return "\n".join(lines)

class BoundaryCondition(ModelForcing):
    """ descriptions of boundary conditions """
    
    # only used if time varying - can also be blank or 'BLOCK'
    time_interpolation='LINEAR' 

    def __init__(self,boundaries,substances,data):
        """
        boundaries: list of string id's of individual boundary exchanges, 
           types of boundaries as strings,
           index (negative from -1) of boundary exchanges
        Strings should not be pre-quoted

        substances: list of string names of substances.

        data: depends on type -
          constant: apply the same value for all items, all substances
          1d array: apply the same value for all items, but different values
            for different substances
          2d array: data[i,j] is for item item i, substance j

          tuple (datetime 1d array,values 2d or 3d array): time series.  t_secs
          gives the time of each set of values.  first dimension of values
          is time, and must match length of t_secs.
           2nd dimension is item  (had been switched with substance)
           3rd dimension is substance

          pandas Series with DatetimeIndex - only works for scalar timeseries.

        datetimes are specified as in Scenario.as_datetime - DateTime instance, integer seconds
         or float datenum.
        """
        super(BoundaryCondition,self).__init__(items=boundaries,substances=substances,data=data)

    bdefs=None
    def fmt_item(self,bdry):
        if self.bdefs is None:
            self.bdefs=self.scenario.hydro.boundary_defs()
        if isinstance(bdry,int):
            bdry=bdefs[-1-bdry]['id']
        return bdry
        
class Discharge(object):
    """ 
    Simple naming for load/withdrawal location.  Most of the
    work is in Load.

    No support yet for things like SURFACE, BANK or BED.
    """
    def __init__(self,
                 seg_id=None, # directly specify a segment
                 element=None,k=0, # segment from element,k combination
                 load_id=None, # defaults to using seg_id
                 load_name=None, # defaults to load_id,
                 load_type=None, # defaults to load_id,
                 option=None): # defaults to 'MASS' substances
        self.scenario=None # will be set by Scenario
        self.element=element
        self.seg_id=seg_id
        self.k=k
        self.load_id=load_id
        self.load_name=load_name
        self.load_type=load_type
        self.option=option
    def update_fields(self):
        """ since some mappings are not available (like segment name => id_
        until we have a scenario, calling this will update relevant fields
        when a scenario is available.
        """
        if self.seg_id is None:
            assert self.element is not None
            self.seg_id=self.scenario.hydro.segment_select(element=self.element,k=self.k)[0]

        self.load_id=self.load_id or "seg-%d"%self.seg_id
        self.load_name=self.load_name or self.load_id
        self.load_type=self.load_type or self.load_id
        self.option=self.option or "MASS"

    def text(self):
        self.update_fields()
        fmt=" {seg} {self.option} '{self.load_id}' '{self.load_name}' '{self.load_type}' "
        return fmt.format(seg=self.seg_id+1, # to 1-based
                          self=self)

class Load(ModelForcing):
    """
    descriptions of mass sources/sinks (loads, withdrawals)
    """
    
    # only used if time varying - can also be blank or 'BLOCK'
    time_interpolation='LINEAR' 

    def __init__(self,discharges, 
                 substances,
                 data):
        """
        see ModelForcing or BoundaryCondition docstring
        """
        super(Load,self).__init__(items=discharges,
                                  substances=substances,
                                  data=data)

    def fmt_item(self,disch):
        if isinstance(disch,int):
            disch=self.scenario.discharges[disch].load_id
        if isinstance(disch,Discharge):
            disch=disch.load_id
        assert(isinstance(disch,str))
        return disch

    # text_substances() from parent class
    # text_data() from parent class
    # text() from parent class


class Parameter(object):
    scenario=None # to be set by the scenario
    def __init__(self,scenario=None,name=None,hydro=None):
        self.name = name or "unnamed"  # may be set later.
        self.scenario=scenario # may be set later
        self._hydro = hydro
        
    @property
    def safe_name(self):
        """ reformatted self.name which can be used in filenames 
        """
        return self.name.replace(' ','_').lower()

    @property
    def hydro(self):
        if self._hydro is not None:
            return self._hydro
        
        try:
            return self.scenario.hydro
        except AttributeError:
            return None
    @hydro.setter
    def hydro(self,value):
        self._hydro=value
    
    def text(self,write_supporting=True):
        """
        write_supporting=True will create any relevant binary files
        at the same time.
        """
        raise WaqException("To be implemented in subclasses")
    def evaluate(self,**kws):
        """ interface is evolving, but roughly, subclasses can
        interpret elements of kws as they wish, returning a presumably
        narrower parameter object.  Example usage would be to take
        a ParameterSpatioTemporal, and evaluate with t=<some time>,
        returning a ParameterSpatial.
        """ 
        return self

class ParameterConstant(Parameter):
    def __init__(self,value,scenario=None,name=None,hydro=None):
        super(ParameterConstant,self).__init__(name=name,scenario=scenario,hydro=hydro)
        self.data=self.value=value

    def text(self,write_supporting=True):
        return "CONSTANTS  '{}'  DATA {:.5e}".format(self.name,self.value)


class ParameterSpatial(Parameter):
    """ Process parameter which varies only in space - same 
    as DWAQ's 'PARAMETERS'
    """
    def __init__(self,per_segment=None,par_file=None,scenario=None,name=None,hydro=None):
        super(ParameterSpatial,self).__init__(name=name,scenario=scenario,hydro=hydro)
        if par_file is not None:
            self.par_file=par_file
            with open(par_file,'rb') as fp:
                fp.read(4) # toss zero timestamp
                # no checks for proper size....living on the edge
                per_segment=np.fromfile(fp,'f4')
        self.data=per_segment

    @property
    def supporting_file(self):
        """ base name of the supporting binary file (dir name will come from scenario)
        """
        return self.scenario.name + "-" + self.safe_name + ".par"
    def text(self,write_supporting=True):
        if write_supporting:
            self.write_supporting()
        return "PARAMETERS '{self.name}' ALL BINARY_FILE '{self.supporting_file}'".format(self=self)
    def write_supporting(self):
        with open(os.path.join(self.scenario.base_path,self.supporting_file),'wb') as fp:
            # leading 'i4' with value 0.
            # I didn't see this in the docs, but it's true of the 
            # .par files written by the GUI. probably this is to make the format
            # the same as a segment function with a single time step.
            fp.write(np.array(0,dtype='i4').tobytes())
            fp.write(self.data.astype('f4').tobytes())
    def evaluate(self,**kws):
        if 'seg' in kws:
            return ParameterConstant( self.data[kws.pop('seg')] )
        else:
            return self

class ParameterTemporal(Parameter):
    """ Process parameter which varies only in time
    aka DWAQ's FUNCTION
    """
    def __init__(self,times,values,scenario=None,name=None,hydro=None):
        """
        times: [N] sized array, 'i4', giving times as seconds after time0
        values: [N] sized array, 'f4', giving function values.
        """
        super(ParameterTemporal,self).__init__(name=name,scenario=scenario,hydro=hydro)
        self.times=times
        self.values=values
    def text(self):
        lines=["FUNCTIONS '{}' BLOCK DATA".format(self.name),
               ""]
        for t,v in zip(self.times,self.values):
            lines.append("{}  {:e}".format(self.scenario.fmt_datetime(t),v) )
        return "\n".join(lines)
    def evaluate(self,**kws):
        if 't' in kws:
            t=kws.pop('t')
            tidx=np.searchsorted(self.times,t)
            return ParameterConstant( self.values[tidx] )
        else:
            return self


class ParameterSpatioTemporal(Parameter):
    """ Process parameter which varies in time and space - aka DWAQ 
    SEG_FUNCTIONS
    """
    interpolation='LINEAR' # or 'BLOCK'

    def __init__(self,times=None,values=None,func_t=None,scenario=None,name=None,
                 seg_func_file=None,enable_write_symlink=False,n_seg=None,
                 hydro=None):
        """
        times: [N] sized array, 'i4', giving times in system clock units
          (typically seconds after time0)
        values: [N,n_seg] array, 'f4', giving function values

        or func_t, which takes a time as 'i4', and returns the values for
        that moment

        or seg_func_file, a path to an existing file.  if enable_write_symlink
          is True, then write_supporting() will symlink to this file.  otherwise
          it is copied.

        note that on write(), a subset of the times may be used based on 
        start/stop times of the associated scenario.  Still, on creation, should
        pass the full complement of times for which data exists (of course consistent
        with the shape of data when explicit data is passed)
        """
        if seg_func_file is None:
            assert(times is not None)
            assert(values is not None or func_t is not None)
        super(ParameterSpatioTemporal,self).__init__(name=name,scenario=scenario,hydro=hydro)
        self.func_t=func_t
        self._times=times
        self.values=values
        self.seg_func_file=seg_func_file
        self.enable_write_symlink=enable_write_symlink
        self._n_seg=n_seg # only needed for evaluate() when scenario isn't set

    # goofy helpers when n_seg or times can only be inferred after instantiation
    @property
    def n_seg(self):
        if (self._n_seg is None):
            try:
                # awkward reference to hydro.
                self._n_seg = self.hydro.n_seg
            except AttributeError:
                pass
        return self._n_seg

    @property
    def times(self):
        if (self._times is None) and (self.seg_func_file is not None):
            stride=4+self.n_seg*4
            nbytes=os.stat(self.seg_func_file).st_size
            frames=nbytes//stride
            self._times=np.zeros(frames,'i4')
            with open(self.seg_func_file,'rb') as fp:
                for ti in range(frames):
                    fp.seek(stride*ti)
                    self._times[ti]=np.fromstring(fp.read(4),'i4')
        return self._times

    @property
    def supporting_file(self):
        """ base name of the supporting binary file, (no dir. name) """
        return self.scenario.name + "-" + self.safe_name + ".seg"
    @property
    def supporting_path(self):
        return os.path.join(self.scenario.base_path,self.supporting_file)
    
    def text(self,write_supporting=True):
        if write_supporting:
            self.write_supporting()
        return ("SEG_FUNCTIONS '{self.name}' {self.interpolation}"
                " ALL BINARY_FILE '{self.supporting_file}'").format(self=self)
    def write_supporting_try_symlink(self):
        if self.seg_func_file is not None:
            if self.enable_write_symlink:
                rel_symlink(self.seg_func_file,self.supporting_path)
            else:
                shutil.copyfile(self.seg_func_file,self.supporting_path)
            return True
        else:
            return False
        
    def write_supporting(self):
        if self.write_supporting_try_symlink():
            return
        
        target=os.path.join(self.scenario.base_path,self.supporting_file)

        # limit to the time span of the scenario
        tidxs=np.arange(len(self.times))
        datetimes=self.times*self.scenario.scu + self.scenario.time0
        start_i,stop_i = np.searchsorted(datetimes,
                                         [self.scenario.start_time,
                                          self.scenario.stop_time])
        start_i=max(0,start_i-1)
        stop_i =min(stop_i+1,len(tidxs))
        tidxs=tidxs[start_i:stop_i]
        msg="write_supporting: only writing %d of %d timesteps. what a savings!"%(len(tidxs),
                                                                                  len(self.times))
        self.scenario.log.info(msg)

        # This is split out so that the parallel implementation can jump in just at this
        # point
        with open(target,'wb') as fp:
            self.write_supporting_loop(tidxs,fp)

    def write_supporting_loop(self,tidxs,fp):
        t_secs=self.times.astype('i4')
        
        for tidx in tidxs:
            t=t_secs[tidx]
            fp.write(t_secs[tidx].tobytes())
            if self.values is not None:
                values=self.values[tidx,:]
            else:
                values=self.func_t(t)
            fp.write(values.astype('f4').tobytes())

    def evaluate(self,**kws):
        # This implementation is pretty rough - 
        # this class is really a mix of
        # ParameterSpatial and ParameterTemporal, yet it duplicates
        # the code from both of those here.

        if self.seg_func_file is not None:
            if 't' in kws:
                t=kws.pop('t')
                stride=4+self.n_seg*4
                ti=np.searchsorted(self.times[:-1],t)
                if self.times[ti]!=t:
                    print("Mismatch in seg func: request=%s  file=%s"%(t,self.times[ti]))
                with open(self.seg_func_file,'rb') as fp:
                    fp.seek(stride*ti+4)
                    values=np.fromfile(fp,'f4',self.n_seg)
                    param=ParameterSpatial(values)
                    return param.evaluate(**kws)
            return self
                
        param=self

        if 't' in kws:
            t=kws.pop('t')
            if self.values is not None:
                tidx=np.searchsorted(self.times,t)
                param=ParameterSpatial( self.values[tidx,:] )
            elif self.func_t is not None:
                param=ParameterSpatial( self.func_t(t) )
        elif 'seg' in kws:
            seg=kws.pop('seg')
            param=ParameterTemporal(times=self.times,values=self.values[:,seg])
        if param is not self:
            # allow other subclasses to do fancier things
            return param.evaluate(**kws)
        else:
            return self
        
    def lowpass(self,lp_secs,volume_threshold=1.0,pad_mode='constant'):
        """
        segments with a volume less than the given threshold are removed 
        from the filtering, replaced by linear interpolation.
        pad_mode: 'constant' pads the time series with the first/last values
                  'zero' pads with zeros.
        """
        dt=np.median(np.diff(self.times))
        if dt>lp_secs:
            return self

        # brute force - load all of the data at once.
        # for a year of 30 minute data over 4k segments,
        # loading the data takes 10s, filtering takes 10s.
        values=[]

        for ti,t in enumerate(self.times):
            if ti%5000==0:
                print("%d / %d"%(ti,len(self.times)))
            spatial=self.evaluate(t=t).data
            if volume_threshold>0:
                volumes=self.hydro.volumes(t)
                mask=(volumes<volume_threshold)
                spatial=spatial.copy() # in case evaluate gave us a reference/view
                spatial[mask] = np.nan
            values.append(spatial)

        values=np.array(values)

        npad=int(5*lp_secs / dt)
        pad =np.ones(npad)

        for seg in range(self.n_seg):
            if pad_mode=='constant':
                prepad=values[0,seg] * pad
                postpad=values[-1,seg] * pad
            elif pad_mode=='zero':
                prepad=postpad=0*pad
            else:
                raise Exception("Bad pad_mode: %s"%pad_mode)
            padded=np.concatenate( ( prepad, 
                                     values[:,seg],
                                     postpad) )
            if volume_threshold>0:
                utils.fill_invalid(padded)
            # possible, especially with a dense-output z-level model
            # where some segments are below the bed and thus always nan
            # that there are still nans hanging out.  so explicitly call
            # them -999
            if np.isnan(padded[0]):
                values[:,seg]=-999
            else:
                lp_values=filters.lowpass(padded,
                                            cutoff=lp_secs,dt=dt)
                values[:,seg]=lp_values[npad:-npad] # trim the pad
        return ParameterSpatioTemporal(times=self.times,
                                       values=values,
                                       enable_write_symlink=False,
                                       n_seg=self.n_seg,
                                       # these probably get overwritten anyway.
                                       scenario=self.scenario,
                                       name=self.name)


# Options for defining parameters and substances:
# 1. as before - list attributes of the class
#    this is annoying because you can't alter the lists easily/safely 
#    until after object instantiation
# 2. as a method, returning a list or dict.  This gets closer, but 
#    then you can't modify values - it's stuck inside a method
# 3. init_* methods called on instantiation.  Just have to be clear
#    about the order of steps, what information is available when, etc.
#    you have just as much information available as when defining things
#    at class definition time.

class NamedObjects(OrderedDict):
    """ 
    utility class for managing collections of objects which
    get a name and a reference to the scenario or hydro
    """
    def __init__(self,sort_key=None,**kw):
        super(NamedObjects,self).__init__()
        assert len(kw)==1
        
        self.parent_name=list(kw.keys())[0]
        self.parent=kw[self.parent_name]
        
        self.sort_key=sort_key

    def normalize_key(self,k):
        try:
            return k.lower()
        except AttributeError:
            return k
    def __setitem__(self,k,v):
        v.name=k
        setattr(v,self.parent_name,self.parent) # v.scenario=self.scenario
        super(NamedObjects,self).__setitem__(self.normalize_key(k),v)
    def __getitem__(self,k):
        return super(NamedObjects,self).__getitem__(self.normalize_key(k))
    def __delitem__(self,k):
        return super(NamedObjects,self).__delitem__(self.normalize_key(k))
        
    def clear(self):
        for key in list(self.keys()):
            del self[key]

    def __iter__(self):
        """ optionally applies an extra level of sorting based on
        self.sort_key
        """
        orig=super(NamedObjects,self).__iter__()
        if self.sort_key is None:
            return orig
        else:
            # is this sort stable? as of python 2.2, yes!
            entries=list(orig)
            real_sort_key=lambda k: self.sort_key(self[k])
            entries.sort( key=real_sort_key )
            return iter(entries)
    # other variants are defined in terms of iter, so only
    # have to change the ordering in __iter__.
    # except that might have changed...
    def values(self):
        return [self[k] for k in self]
    def __add__(self,other):
        a=NamedObjects(**{self.parent_name:self.parent})
        for src in self,other:
            for v in src.values():
                a[v.name]=v
        return a

    # would be nice to change __iter__ behavior since name
    # is already an attribute on the items, but __iter__ is
    # central to the inner workings of other dict methods, and
    # hard to override safely.

class DispArray(object):
    """
    input file manual appendix, page 90 says dispersions file is
    time[i4], [ndisp,nqt] matrix of 'f4' - for each time step.
    nqt is total number of exchanges.
    """
    def __init__(self,name=None,substances=None,data=None):
        """
        typ. usage:
        scenario.dispersions['subtidal_K']=DispArray(substances='.*',data=xxx)

        name: label for the dispersion array, max len 20 char
        substances: list of substance names or patterns for which this array applies.
          can be a str, which is coerced to [str].  Interpreted as regular expression.
        data: working on it.
        """
        if name is not None:
            self.name=name[:20]
        else:
            self.name=None
        if isinstance(substances,str):
            substances=[substances]
        self.patts=substances
        self.data=data
    def matches(self,name):
        for patt in self.patts:
            if re.match(patt,name):
                return True
        return False
    def text(self,write_supporting=True):
        if write_supporting:
            self.write_supporting()
        return "XXX PARAMETERS '{self.name}' ALL BINARY_FILE '{self.supporting_file}'".format(self=self)
    @property
    def safe_name(self):
        """ reformatted self.name which can be used in filenames 
        """
        return self.name.replace(' ','_')
    def supporting_file(self):
        """ base name of the supporting binary file (dir name will come from scenario)
        """
        return self.scenario.name + "-" + self.safe_name + ".par"

    def write_supporting(self):
        with open(os.path.join(self.scenario.base_path,self.supporting_file),'wb') as fp:
            # leading 'i4' with value 0.
            # I didn't see this in the docs, but it's true of the 
            # .par files written by the GUI. probably this is to make the format
            # the same as a segment function with a single time step.
            fp.write(np.array(0,dtype='i4').tobytes())
            fp.write(self.data.astype('f4').tobytes())
        

def map_nef_names(nef):
    subst_names=nef['DELWAQ_PARAMS'].getelt('SUBST_NAMES',[0])

    elt_map={}
    real_map={} # map nc variable names to original names
    new_count=defaultdict(lambda: 0) # map base names to counts
    for i,name in enumerate(subst_names):
        name=name.decode()
        new_name=qnc.sanitize_name(name.strip()).lower()
        new_count[new_name]+=1
        if new_count[new_name]>1:
            new_name+="%03i"%(new_count[new_name])
        elt_map['SUBST_%03i'%(i+1)] = new_name
        real_map[new_name]=name
    return elt_map,real_map

        
DEFAULT='_DEFAULT_'    
class Scenario(scriptable.Scriptable):
    name="tbd" # this is used for the basename of the various files.
    desc=('line1','line2','line3')

    # system clock unit. 
    time0=None
    # time0=datetime.datetime(1990,8,5) # defaults to hydro value
    scu=datetime.timedelta(seconds=1)
    time_step=None # will be taken from the hydro, unless specified otherwise

    log=logging # take a stab at managing messages via logging

    # backward differencing,
    # .60 => second and third keywords are set 
    # => no dispersion across open boundary
    # => lower order at boundaries
    integration_option="15.60"

    #  add quantities to default
    DEFAULT=DEFAULT
    mon_output= (DEFAULT,'SURF','LocalDepth') # monitor file
    grid_output=('SURF','LocalDepth')              # grid topo
    hist_output=(DEFAULT,'SURF','LocalDepth') # history file
    map_output =(DEFAULT,'SURF','LocalDepth')  # map file

    # settings related to paths - a little sneaky, to allow for shorthand
    # to select the next non-existing subdirectory by setting base_path to
    # "auto"
    _base_path="dwaq"
    @property
    def base_path(self):
        return self._base_path
    @base_path.setter
    def base_path(self,v):
        if v=='auto':
            self._base_path=self.auto_base_path()
        else:
            self._base_path=v

    # tuples of name, segment id
    # some confusion about whether that's a segment id or list thereof
    # monitor_areas=[ ('single (0)',[1]),
    #                 ('single (1)',[2]),
    #                 ('single (2)',[3]),
    #                 ('single (3)',[4]),
    #                 ('single (4)',[5]) ]
    # dwaq bug(?) where having transects but no monitor_areas means history
    # file with transects is not written.  so always include a dummy:
    # the output code handles adding 1, so these should be stored zero-based.
    monitor_areas=( ('dummy',[0]), ) 

    # e.g. ( ('gg_outside', [24,26,-21,-27,344] ), ...  )
    # where negative signs mean to flip the sign of that exchange.
    # note that these exchanges are ONE-BASED - this is because the
    # sign convention is wrapped into the sign, so a zero exchange would
    # be ambiguous.
    monitor_transects=()

    base_x_dispersion=1.0 # m2/s
    base_y_dispersion=1.0 # m2/s
    base_z_dispersion=1e-7 # m2/s

    # these default to simulation start/stop/timestep
    map_start_time=None
    map_stop_time=None
    map_time_step=None
    # likewise
    hist_start_time=None
    hist_stop_time=None
    hist_time_step=None
    # and more
    mon_start_time=None
    mon_stop_time=None
    mon_time_step=None

    def __init__(self,hydro,**kw):
        self.log=logging.getLogger(self.__class__.__name__)
        
        self.dispersions=NamedObjects(scenario=self)

        if hydro:
            print( "Setting hydro")
            self.set_hydro(hydro)
        else:
            self.hydro=None # will have limited functionality

        self.inp=InpFile(scenario=self)

        # set attributes here, before the init code might want to
        # use these settings (e.g. start/stop times)
        for k,v in iteritems(kw):
            try:
                getattr(self,k)
                setattr(self,k,v)
            except AttributeError:
                raise Exception("Unknown Scenario attribute: %s"%k)

        self.parameters=self.init_parameters()
        if self.hydro is not None:
            self.hydro_parameters=self.init_hydro_parameters()
        self.substances=self.init_substances()
        self.init_bcs()
        self.init_loads()

    # scriptable interface settings:
    cli_options="hp:"
    def cli_handle_option(self,opt,val):
        if opt=='-p':
            print("Setting base_path to '%s'"%val)
            self.base_path=val
        else:
            super(Scenario,self).cli_handle_option(opt,val)
        
    def auto_base_path(self):
        for c in range(100):
            base_path='dwaq%02d'%c
            if not os.path.exists(base_path):
                return base_path
        else:
            assert False
        
    @property
    def n_substances(self):
        return len(self.substances)
    @property
    def n_active_substances(self):
        return len( [sub for sub in self.substances.values() if sub.active] )
    @property
    def n_inactive_substances(self):
        return len( [sub for sub in self.substances.values() if not sub.active] )
    
    @property
    def multigrid_block(self):
        """ 
        inserted verbatim in section 3 of input file.
        """
        # appears that for a lot of processes, hydro must be dense wrt segments
        # exchanges need not be dense, but sparse exchanges necessitate explicitly
        # providing the number of layers.  And that brings us to this stanza:

        # sparse_layers is actually an input flag to the aggregator
        # assert not self.hydro.sparse_layers
        # instead, test this programmatically, and roughly the same as how dwaq will test
        self.hydro.infer_2d_elements()
        kmax=self.hydro.seg_k.max()
        if self.hydro.n_seg != self.hydro.n_2d_elements*(kmax+1):
            raise Exception("You probably mean to be running with segment-dense hydrodynamics")
        
        num_layers=self.hydro.n_seg / self.hydro.n_2d_elements
        if self.hydro.vertical != self.hydro.SIGMA:
            return """MULTIGRID
  ZMODEL NOLAY %d
END_MULTIGRID"""%num_layers
        else:
            return " ; sigma layers - no multigrid stanza"
    
    def set_hydro(self,hydro):
        self.hydro=hydro
        self.hydro.scenario=self

        # sensible defaults for simulation period
        self.time_step=self.hydro.time_step
        self.time0 = self.hydro.time0
        self.start_time=self.time0+self.scu*self.hydro.t_secs[0]
        self.stop_time =self.time0+self.scu*self.hydro.t_secs[-1]

    def init_parameters(self):
        params=NamedObjects(scenario=self)
        params['ONLY_ACTIVE']=ParameterConstant(1) # almost always a good idea.
        
        return params
    def init_hydro_parameters(self):
        """ parameters which come directly from the hydro, and are
        written out in the same way that process parameters are 
        written.
        """
        if self.hydro:
            # in case hydro is re-used, make sure that this call gets a fresh
            # set of parameters.  some leaky abstraction going on...
            return self.hydro.parameters(force=True)
        else:
            self.log.warning("Why requesting hydro parameters with no hydro?")
            assert False # too weird
            return NamedObjects(scenario=self)

    def init_substances(self):
        # sorts active substances first.
        return NamedObjects(scenario=self,sort_key=lambda s: not s.active)

    def text_thatcher_harleman_lags(self):
        return """;
; Thatcher-Harleman timelags
0 ; no lags
        """

    def init_bcs(self):
        self.bcs=[]

    def add_bc(self,*args,**kws):
        bc=BoundaryCondition(*args,**kws)
        bc.scenario=self
        self.bcs.append(bc)
        return bc

    def init_loads(self):
        """
        Set up discharges (locations of point sources/sinks), and
        corresponding loads (e.g. mass/time for a substance at a source)
        """
        self.discharges=[]
        self.loads=[]

    def add_discharge(self,*arg,**kws):
        disch=Discharge(*arg,**kws)
        disch.scenario=self
        self.discharges.append(disch)
        return disch

    def add_load(self,*args,**kws):
        load=Load(*args,**kws)
        load.scenario=self
        self.loads.append(load)
        return load
    
    def add_monitor_from_shp(self,shp_fn,naming='elt_layer'):
        locations=wkb2shp.shp2geom(shp_fn)
        self.hydro.infer_2d_elements()

        g=self.hydro.grid()
        new_areas=[] # generate as list, then assign as tuple
        names={}
        for n,segs in self.monitor_areas:
            names[n]=True # record names in use to avoid duplicates

        for i,rec in enumerate(locations):
            geom=rec['geom']
            # for starters, only points are allowed, but it could
            # be extended to handle lines for transects and polygons
            # for horizontal integration

            if geom.type=='Point':
                xy=np.array(geom.coords)[0]
                elt=g.select_cells_nearest(xy)

                segs=np.nonzero( self.hydro.seg_to_2d_element==elt )[0]
                for layer,seg in enumerate(segs):
                    if naming=='elt_layer':
                        name="elt%d_layer%d"%(elt,layer)
                        if name not in names:
                            new_areas.append( (name,[seg] ) )
                            names[name]=True
            elif geom.type=='Polygon':
                try:
                    name=rec[naming]
                except:
                    name="polygon%d"%i
                    
                # bitmask over 2D elements
                self.log.info("Selecting elements in polygon '%s'"%name)
                # better to go by center, so that non-intersecting polygons
                # yield non-intersecting sets of elements and segments
                elt_sel=g.select_cells_intersecting(geom,by_center=True) # few seconds

                # extend to segments:
                seg_sel=elt_sel[ self.hydro.seg_to_2d_element ] & (self.hydro.seg_to_2d_element>=0)         

                segs=np.nonzero( seg_sel )[0]

                assert name not in names
                new_areas.append( (name,segs) )
                names[name]=True
            else:
                self.log.warning("Not ready to handle geometry type %s"%geom.type)

        self.log.info("Added %d monitored segments from %s"%(len(new_areas),shp_fn))
        self.monitor_areas = self.monitor_areas + tuple(new_areas)

    def add_transects_from_shp(self,shp_fn,naming='count',clip_to_poly=True,
                               on_boundary='warn_and_skip'):
        locations=wkb2shp.shp2geom(shp_fn)
        g=self.hydro.grid()

        if clip_to_poly:
            poly=g.boundary_polygon()
            
        new_transects=[] # generate as list, then assign as tuple

        for i,rec in enumerate(locations):
            geom=rec['geom']

            if geom.type=='LineString':
                if clip_to_poly:
                    clipped=geom.intersection(poly)

                    # rather than assume that clipped comes back with
                    # the same orientation, and multiple pieces come
                    # back in order, manually re-assemble the line
                    if clipped.type=='LineString':
                        segs=[clipped]
                    else:
                        segs=clipped.geoms

                    all_dists=[]
                    for seg in segs:
                        for xy in seg.coords:
                            all_dists.append( geom.project( geometry.Point(xy) ) )
                    # sorting the distances ensures that orientation is same as
                    # original
                    all_dists.sort()

                    xy=[geom.interpolate(d) for d in all_dists]
                else:
                    xy=np.array(geom.coords)
                    
                if naming=='count':
                    name="transect%04d"%i
                else:
                    name=rec[naming]
                exchs=self.hydro.path_to_transect_exchanges(xy,on_boundary=on_boundary)
                new_transects.append( (name,exchs) )
            else:
                self.log.warning("Not ready to handle geometry type %s"%geom.type)
        self.log.info("Added %d monitored transects from %s"%(len(new_transects),shp_fn))
        self.monitor_transects = self.monitor_transects + tuple(new_transects)

    def add_area_boundary_transects(self,exclude='dummy'):
        """
        create monitor transects for the common boundaries between a subset of
        monitor areas. this assumes that the monitor areas are distinct - no
        overlapping cells (in fact it asserts this).
        The method and the non-overlapping requirement apply only for areas which
        do *not* match the exclude regex.
        """
        areas=[a[0] for a in self.monitor_areas]
        if exclude is not None:
            areas=[a for a in areas if not re.match(exclude,a)]

        mon_areas=dict(self.monitor_areas)

        seg_to_area=np.zeros(self.hydro.n_seg,'i4')-1

        for idx,name in enumerate(areas):
            # make sure of no overlap:
            assert np.all( seg_to_area[ mon_areas[name] ] == -1 )
            # and label to this area:
            seg_to_area[ mon_areas[name] ] = idx

        poi0=self.hydro.pointers - 1

        exch_areas=seg_to_area[poi0[:,:2]]
        # fix up negatives in poi0
        exch_areas[ poi0[:,:2]<0 ] = -1

        # convert to tuples so we can get unique pairs
        exch_areas_tupes=set( [ tuple(x) for x in exch_areas if x[0]!=x[1] and x[0]>=0 ] )
        # make the order canonical 
        canon=set()
        for a,b in exch_areas_tupes:
            if a>b:
                a,b=b,a
            canon.add( (a,b) )
        canon=list(canon) # re-assert order

        names=[]
        exch1s=[]

        for a,b in canon:
            self.log.info("%s <-> %s"%(areas[a],areas[b]))
            name=areas[a][:9] + "__" + areas[b][:9]
            self.log.info("  name: %s"%name)
            names.append(name)

            fwd=np.nonzero( (exch_areas[:,0]==a) & (exch_areas[:,1]==b) )[0]
            rev=np.nonzero( (exch_areas[:,1]==a) & (exch_areas[:,0]==b) )[0]
            exch1s.append( np.concatenate( (fwd+1, -(rev+1)) ) )
            self.log.info("  exchange count: %d"%len(exch1s[-1]))

        # and add to transects:
        transects=tuple(zip(names,exch1s))
        self.monitor_transects=self.monitor_transects + transects
        
    def add_transect(self,name,exchanges):
        """ Append a transect definition for logging.
        """
        self.monitor_transects = self.monitor_transects + ( (name,exchanges), )
        
    def ensure_base_path(self):
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path)

    def write_inp(self):
        """
        Write the inp file for delwaq1/delwaq2
        """
        self.ensure_base_path()
        # parameter files are also written along the way
        self.inp.write()

    def write_hydro(self):
        self.ensure_base_path()
        self.hydro.write()

    _pdb=None
    @property
    def process_db(self):
        if self._pdb is None:
            self._pdb = waq_process.ProcessDB(scenario=self)
        return self._pdb
    def lookup_item(self,name):
        return self.process_db.substance_by_id(name)

    def as_datetime(self,t):
        """
        t can be a datetime object, an integer number of seconds since time0,
        or a floating point datenum
        """
        if np.issubdtype(type(t),int):
            return self.time0 + t*self.scu
        elif np.issubdtype(type(t),float):
            return num2date(t)
        elif isinstance(t,datetime.datetime):
            return t
        else:
            raise WaqException("Invalid type for datetime: {}".format(type(t)))

    def fmt_datetime(self,t):
        """ 
        return datetime formatted as text.
        format is part of input file configuration, but 
        for now, stick with 1990/08/15-12:30:00

        t is specified as in as_datetime() above.
        """
        return self.as_datetime(t).strftime('%Y/%m/%d-%H:%M:%S')

    #-- Access to output files
    def nef_history(self):
        hda=os.path.join( self.base_path,self.name+".hda")
        hdf=os.path.join( self.base_path,self.name+".hdf")
        if os.path.exists(hda):
            return nefis.Nefis(hda, hdf)
        else:
            return None
    def nef_map(self):
        ada=os.path.join(self.base_path, self.name+".ada")
        adf=os.path.join(self.base_path, self.name+".adf")
        if os.path.exists(ada):
            return nefis.Nefis( ada,adf)
        else:
            return None

    #  netcdf versions of those:
    def nc_map(self,nc_kwargs={}):
        nef=self.nef_map()
        try:
            elt_map,real_map = map_nef_names(nef)

            nc=nefis_nc.nefis_to_nc(nef,element_map=elt_map,nc_kwargs=nc_kwargs)
            for vname,rname in iteritems(real_map):
                nc.variables[vname].original_name=rname
            # the nefis file does not contain enough information to get
            # time back to a real calendar, so rely on the Scenario's
            # version of time0
            if 'time' in nc.variables:
                nc.time.units='seconds since %s'%self.time0.strftime('%Y-%m-%d %H:%M:%S')
        finally:
            nef.close()
        return nc

    # try to find the common chunks of code between writing ugrid
    # nc output and the history output

    def ugrid_map(self,nef=None,nc_kwargs={}):
        return self.ugrid_nef(mode='map',nef=nef,nc_kwargs=nc_kwargs)

    def ugrid_history(self,nef=None,nc_kwargs={}):
        return self.ugrid_nef(mode='history',nef=nef,nc_kwargs=nc_kwargs)

    default_ugrid_output_settings=['quickplot_compat']
    
    def ugrid_nef(self,mode='map',nef=None,nc_kwargs={},output_settings=None):
        """ Like nc_map, but write a netcdf file more ugrid compliant.
        this is actually pretty different, as ugrid requires that 3D
        field is organized into a horizontal dimension (i.e element)
        and vertical dimension (layer).  the original nefis code
        just gives segment.
        nef: supply an already open nef.  Note that caller is responsible for closing it!
        mode: 'map' use the map output
              'history' use history output
        """
        if output_settings is None:
            output_settings=self.default_ugrid_output_settings


        if nef is None:
            if mode is 'map':
                nef=self.nef_map()
            elif mode is 'history':
                nef=self.nef_history()
            close_nef=True
        else:
            close_nef=False
        if nef is None: # file didn't exist
            self.log.info("NEFIS file didn't exist. Skipping ugrid_nef()")
            return None
            
        flowgeom=self.flowgeom()
        mesh_name="FlowMesh" # sync with sundwaq for now.

        if flowgeom is not None:
            nc=flowgeom.copy(**nc_kwargs)
        else:
            nc=qnc.empty(**nc_kwargs)
        nc._set_string_mode('fixed') # required for writing to disk

        self.hydro.infer_2d_elements()

        try:
            if mode is 'map':
                seg_k = self.hydro.seg_k
                seg_elt = self.hydro.seg_to_2d_element
                n_locations=len(seg_elt)
            elif mode is 'history':
                # do we go through the location names, trying to pull out elt_k? no - as needed,
                # use self.monitor_areas.

                # maybe the real question is how will the data be organized in the output?
                # if each history output can be tied to a single segment, that's one thing.
                # Could subset the grid, or keep the full grid and pad the data with fillvalue.
                # but if some/all of the output goes to multiple segments, then what?
                # keep location_name in the output?
                # maybe we skip any notion of ugrid, and instead follow a more CF observed features
                # structure?

                # also, whether from the original Scenario, or by reading in the inp file, we can get
                # the original map between location names and history outputs
                # for the moment, classify everything in the file based on the first segment
                # listed

                # try including the full grid, and explicitly output the mapping between history
                # segments and the 2D+z grid.
                hist_segs=[ma[1][0] for ma in self.monitor_areas]
                seg_k=self.hydro.seg_k[ hist_segs ]
                seg_elt=self.hydro.seg_to_2d_element[hist_segs]

                # Need to handle transects - maybe that's handled entirely separately.
                # even so, the presence of transects will screw up the matching of dimensions
                # below for history output.
                # pdb.set_trace()

                # i.e. current output with transect:
                # len(seg_elt)==135
                # but we ended up creating an anonymous dimension d138
                # (a) could consult scenario to get the count of transects
                # (b) is there anything in the NEFIS file to indicate transect output?
                # (c) could use the names as a hint
                #     depends on the input file, but currently have things like eltNNN_layerNN
                #     vs. freeform for transects

                # what about just getting the shape from the LOCATIONS field?
                shape,dtype = nef['DELWAQ_PARAMS'].getelt('LOCATION_NAMES',shape_only=True)
                n_locations=shape[1]
                if n_locations>len(seg_elt):
                    self.log.info("Looks like there were %d transects, too?"%(n_locations - len(seg_elt)))
                elif n_locations<len(seg_elt):
                    self.log.warning("Weird - fewer output locations than anticipated! %d vs %d"%(n_locations,
                                                                                                  len(seg_elt)))
            else:
                assert False

            n_layers= seg_k.max() + 1

            # elt_map: 'SUBST_001' => 'oxy'
            # real_map: 'saturoxy' => 'SaturOXY'
            elt_map,real_map = map_nef_names(nef)

            # check for unique element names
            name_count=defaultdict(lambda: 0)
            for group in nef.groups():
                for elt_name in group.cell.element_names:
                    name_count[elt_name]+=1

            # check for unique unlimited dimension:
            n_unl=0
            for group in nef.groups():
                # there are often multiple unlimited dimensions.
                # hopefully just 1 unlimited in the RESULTS group
                if 0 in group.shape and group.name=='DELWAQ_RESULTS':
                    n_unl+=1

            # give the user a sense of how many groups are being
            # written out:
            self.log.info("Elements to copy from NEFIS:")
            for group in nef.groups():
                for elt_name in group.cell.element_names:
                    nef_shape,nef_type=group.getelt(elt_name,shape_only=True)
                    vshape=group.shape + nef_shape
                    self.log.info("  %s.%s: %s (%s)"%(group.name,
                                                      elt_name,
                                                      vshape,nef_type))

            for group in nef.groups():
                g_shape=group.shape
                grp_slices=[slice(None)]*len(g_shape)
                grp_dim_names=[None]*len(g_shape)

                # infer that an unlimited dimension in the RESULTS
                # group is time.
                if 0 in g_shape and group.name=='DELWAQ_RESULTS':
                    idx=list(g_shape).index(0)
                    if n_unl==1: # which will be named
                        grp_dim_names[idx]='time'

                for elt_name in group.cell.element_names:
                    # print("elt name is",elt_name)

                    # Choose a variable name for this element
                    if name_count[elt_name]==1:
                        vname=elt_name
                    else:
                        vname=group.name + "_" + elt_name

                    if vname in elt_map:
                        vname=elt_map[vname]
                    else:
                        vname=vname.lower()

                    self.log.info("Writing variable %s"%vname)
                    subst=self.lookup_item(vname) # may be None!
                    if subst is None:
                        self.log.info("No metadata from process library on %s"%repr(vname))

                    # START time-iteration HERE
                    # for large outputs, need to step through time
                    # assume that only groups with 'time' as a dimension
                    # (as detected above) need to be handled iteratively.
                    # 'time' assumed to be part of group shape.
                    # safe to always iterate on time.

                    # nef_shape is the shape of the element subject to grp_slices,
                    # as understood by nefis, before squeezing or projecting to [cell,layer]
                    nef_shape,value_type=group.getelt(elt_name,shape_only=True)
                    self.log.debug("nef_shape: %s"%nef_shape )
                    self.log.debug("value_type: %s"%value_type )

                    if value_type.startswith('f'):
                        fill_value=np.nan
                    elif value_type.startswith('S'):
                        fill_value=None
                    else:
                        fill_value=-999

                    nef_to_squeeze=[slice(None)]*len(nef_shape)
                    if 1: # squeeze unit element dimensions
                        # iterate over just the element portion of the shape
                        squeeze_shape=list( nef_shape[:len(g_shape)] )
                        for idx in range(len(g_shape),len(nef_shape)):
                            if nef_shape[idx]==1:
                                nef_to_squeeze[idx]=0
                            else:
                                squeeze_shape.append(nef_shape[idx])
                    else: # no squeeze
                        squeeze_shape=list(nef_shape)

                    self.log.debug("squeeze_shape: %s"%squeeze_shape)
                    self.log.debug("nef_to_squeeze: %s"%nef_to_squeeze)

                    # mimics qnc naming - will come back to expand 3D fields
                    # and names
                    dim_names=[qnc.anon_dim_name(size=l) for l in squeeze_shape]
                    for idx,name in enumerate(grp_dim_names): # okay since squeeze only does elt dims
                        if name:
                            dim_names[idx]=name

                    # special handling for results, which need to be mapped 
                    # back out to 3D
                    proj_shape=list(squeeze_shape)
                    if group.name=='DELWAQ_RESULTS' and self.hydro.n_seg in squeeze_shape:
                        seg_idx = proj_shape.index(self.hydro.n_seg)
                        proj_shape[seg_idx:seg_idx+1]=[self.hydro.n_2d_elements,
                                                       n_layers]
                        # the naming of the layers dimension matches assumptions in ugrid.py
                        # not sure how this is supposed to be specified
                        dim_names[seg_idx:seg_idx+1]=["nFlowElem","nFlowMesh_layers"]

                        # new_value=np.zeros( new_shape, value_type )
                        # new_value[...]=fill_value

                        # this is a little tricky, but seems to work.
                        # map segments to (elt,layer), and all other dimensions
                        # get slice(None).
                        # vmap assumes no group slicing, and is to be applied
                        # to the projected array (projection does not involve
                        # any slices on the nefis src side)
                        vmap=[slice(None) for _ in proj_shape]
                        vmap[seg_idx]=seg_elt
                        vmap[seg_idx+1]=seg_k
                        # new_value[vmap] = value
                    elif group.name=='DELWAQ_RESULTS' and n_locations in squeeze_shape:
                        # above and below: n_locations used to be len(seg_elt)
                        # but it's still writing out things like location_names with
                        # an anonymous dimension
                        seg_idx = proj_shape.index(n_locations)
                        # note that nSegment is a bit of a misnomer, might have some transects
                        # in there, too.
                        dim_names[seg_idx]="nSegment"
                    else:
                        vmap=None # no projection

                    for dname,dlen in zip(dim_names,proj_shape):
                        if dname=='time':
                            # if time is not specified as unlimited, it gets
                            # included as the fastest-varying dimension, which
                            # makes writes super slow.
                            nc.add_dimension(dname,0)
                        else:
                            nc.add_dimension(dname,dlen)

                    # most of the time goes into writing.
                    # typically people optimize chunksize, but HDF5 is
                    # throwing an error when time chunk>1, so it's
                    # hard to imagine any improvement over the defaults.
                    ncvar=nc.createVariable(vname,np.dtype(value_type),dim_names,
                                            fill_value=fill_value,
                                            complevel=2,
                                            zlib=True)

                    if vmap is not None:
                        nc.variables[vname].mesh=mesh_name
                        # these are specifically the 2D horizontal metadata
                        if 'quickplot_compat' in output_settings:
                            # as of Delft3D_4.01.01.rc.03, quickplot only halfway understands
                            # ugrid, and actually does better when location is not specified.
                            self.log.info('Dropping location for quickplot compatibility')
                        else:
                            nc.variables[vname].location='face' # 
                        nc.variables[vname].coordinates="FlowElem_xcc FlowElem_ycc"

                    if subst is not None:
                        if hasattr(subst,'unit'):
                            # in the process table units are parenthesized
                            units=subst.unit.replace('(','').replace(')','')
                            # no guarantee of CF compliance here...
                            nc.variables[vname].units=units
                        if hasattr(subst,'item_nm'):
                            nc.variables[vname].long_name=subst.item_nm
                        if hasattr(subst,'aggrega'):
                            nc.variables[vname].aggregation=subst.aggrega
                        if hasattr(subst,'groupid'):
                            nc.variables[vname].group_id=subst.groupid

                    if 'time' in dim_names:
                        # only know how to deal with time as the first index
                        assert dim_names[0]=='time'
                        self.log.info("Will iterate over %d time steps"%proj_shape[0])

                        total_tic=t_last=time.time()
                        read_sum=0
                        write_sum=0
                        for ti in range(proj_shape[0]):
                            read_sum -= time.time()
                            value_slice=group.getelt(elt_name,[ti])
                            read_sum += time.time()

                            if vmap is not None:
                                proj_slice=np.zeros(proj_shape[1:],value_type)
                                proj_slice[...]=fill_value
                                proj_slice[tuple(vmap[1:])]=value_slice
                            else:
                                proj_slice=value_slice
                            write_sum -= time.time()
                            ncvar[ti,...] = proj_slice
                            write_sum += time.time()

                            if (time.time() - t_last > 2) or (ti+1==proj_shape[0]):
                                t_last=time.time()
                                self.log.info('  time step %d / %d'%(ti,proj_shape[0]))
                                self.log.info('  time for group so far: %fs'%(t_last-total_tic))
                                self.log.info('  reading so far: %fs'%(read_sum))
                                self.log.info('  writing so far: %fs'%(write_sum))

                    else:
                        value=group.getelt(elt_name)
                        if vmap is not None:
                            proj_value=value[tuple(vmap)]
                        else:
                            proj_value=value
                        # used to have extraneous[?] names.append(Ellipsis)
                        ncvar[:]=proj_value

                    setattr(ncvar,'group_name',group.name)
            ####

            for vname,rname in iteritems(real_map):
                nc.variables[vname].original_name=rname
            # the nefis file does not contain enough information to get
            # time back to a real calendar, so rely on the Scenario's
            # version of time0
            if 'time' in nc.variables:
                nc.time.units='seconds since %s'%self.time0.strftime('%Y-%m-%d %H:%M:%S')
                nc.time.standard_name='time'
                nc.time.long_name='time relative to model time0'

        finally:
            if close_nef:
                nef.close()

        # cobble together surface h, depth info.
        if 'time' in nc.variables:
            t=nc.time[:]
        else:
            t=None # not going to work very well...or at all

        z_bed=nc.FlowElem_bl[:]
        # can't be sure of what is included in the output, so have to try some different
        # options

        if 1:
            if mode is 'map':
                etavar=nc.createVariable('eta',np.float32,['time','nFlowElem'],
                                         zlib=True)
                etavar.standard_name='sea_surface_height_above_geoid'
                etavar.mesh=mesh_name

                for ti in range(len(nc.dimensions['time'])):
                    # due to a possible DWAQ bug, we have to be very careful here
                    # depths in dry upper layers are left at their last-wet value,
                    # and count towards totaldepth and localdepth.  That's fixed in
                    # DWAQ now.
                    if 'totaldepth' in nc.variables:
                        depth=nc.variables['totaldepth'][ti,:,0]
                    elif 'depth' in nc.variables:
                        depth=np.nansum(nc.variables['depth'][ti,:,:],axis=2)
                    else:
                        # no freesurface info.
                        depth=-z_bed[None,:] 

                    z_surf=z_bed + depth
                    etavar[ti,:]=z_surf
            elif mode is 'history':
                # tread carefully in case there is nothing useful in the history file.
                if 'totaldepth' in nc.variables and 'nSegment' in nc.dimensions:
                    etavar=nc.createVariable('eta',np.float32,['time',"nSegment"],
                                             zlib=True)
                    etavar.standard_name='sea_surface_height_above_geoid'

                    # some duplication when we have multiple layers of the
                    # same watercolumn
                    # can only create eta for history output, nan for transects
                    pad=np.nan*np.ones(n_locations-len(seg_elt),'f4')
                    for ti in range(len(nc.dimensions['time'])):
                        depth=nc.variables['totaldepth'][ti,:]
                        z_surf=z_bed[seg_elt]
                        z_surf=np.concatenate( (z_surf,pad) )+depth
                        etavar[ti,:]=z_surf
                else:
                    self.log.info('Insufficient info in history file to create eta')

        if 1: # extra mapping info for history files
            pad=-1*np.ones( n_locations-len(seg_elt),'i4')
            if mode is 'history':
                nc['element']['nSegment']=np.concatenate( (seg_elt,pad) )
                nc['layer']['nSegment']=np.concatenate( (seg_k,pad) )
                if flowgeom:
                    xcc=flowgeom.FlowElem_xcc[:]
                    ycc=flowgeom.FlowElem_ycc[:]
                    nc['element_x']['nSegment']=np.concatenate( (xcc[seg_elt],np.nan*pad) )
                    nc['element_y']['nSegment']=np.concatenate( (ycc[seg_elt],np.nan*pad) )

        # extra work to make quickplot happy
        if mode is 'map' and 'quickplot_compat' in output_settings:
            # add in some attributes and fields which might make quickplot happier
            # Add in node depths
            g=unstructured_grid.UnstructuredGrid.from_ugrid(nc)

            # dicey - assumes particular names for the fields:
            if 'FlowElem_zcc' in nc and 'Node_z' not in nc:
                self.log.info('Adding a node-centered depth via interpolation')
                nc['Node_z']['nNode']=g.interp_cell_to_node(nc.FlowElem_zcc[:])
                nc.Node_z.units='m'
                nc.Node_z.positive='up',
                nc.Node_z.standard_name='sea_floor_depth',
                nc.Node_z.long_name="Bottom level at net nodes (flow element's corners)"
                nc.Node_z.coordinates=nc[mesh_name].node_coordinates
                
            # need spatial attrs for node coords
            node_x,node_y = nc[mesh_name].node_coordinates.split()
            nc[node_x].units='m'
            nc[node_x].long_name='x-coordinate of net nodes'
            nc[node_x].standard_name='projection_x_coordinate'
            
            nc[node_y].units='m',
            nc[node_y].long_name='y-coordinate of net nodes'
            nc[node_y].standard_name='projection_y_coordinate'

            for k,v in six.iteritems(nc.variables):
                if 'location' in v.ncattrs():
                    self.log.info("Stripping location attribute from %s for quickplot compatibility"%k)
                    v.delncattr('location')

                # some of the grids being copied through are missing this, even though waq_scenario
                # is supposed to write it out.
                if 'standard_name' in v.ncattrs() and v.standard_name=='ocean_sigma_coordinate':
                    v.formula_terms="sigma: nFlowMesh_layers eta: eta depth: FlowElem_bl"
                    
        return nc

    _flowgeom=None
    def flowgeom(self):
        """ Returns a netcdf dataset with the grid geometry, or None
        if the data is not around.
        """
        if self._flowgeom is None:
            fn=os.path.join(self.base_path,self.hydro.flowgeom_filename)
            if os.path.exists(fn):
                self._flowgeom=qnc.QDataset(fn)
        
        return self._flowgeom

    #-- Command line access
    def cmd_write_runid(self):
        """
        Label run name in the directory, needed for some delwaq2 (confirm?)
        """
        self.ensure_base_path()

        if self.use_bloom:
            with open(os.path.join(self.base_path,'runid.eco'),'wt') as fp:
                fp.write("{}\n".format(self.name))
                fp.write("y\n")
        else:
            with open(os.path.join(self.base_path,'runid.waq'),'wt') as fp:
                fp.write("{}\n".format(self.name))
                fp.write("y\n") # maybe unnecessary for non-bloom run.

    def cmd_write_bloominp(self):
        """
        Copy supporting bloominp file for runs using BLOOM algae
        """
        shutil.copyfile(self.original_bloominp_path,
                        os.path.join(self.base_path,'bloominp.d09'))

    def cmd_write_inp(self):
        """
        Write inp file and supporting files (runid, bloominp.d09) for
        delwaq1/2
        """
        self.ensure_base_path()

        self.log.debug("Writing inp file")
        self.write_inp()

        self.cmd_write_bloominp()
        self.cmd_write_runid()

    def cmd_write_hydro(self):
        """
        Create hydrodynamics data ready for input to delwaq1
        """

        self.ensure_base_path()

        self.log.info("Writing hydro data")
        self.write_hydro()

    def cmd_default(self):
        """
        Prepare all inputs for delwaq1 (hydro, runid, inp files)
        """
        self.cmd_write_hydro()
        self.cmd_write_inp()

    def cmd_write_nc(self):
        """ Transcribe NEFIS to NetCDF for a completed DWAQ run 
        """
        if 1:
            nc2_fn=os.path.join(self.base_path,'dwaq_hist.nc')
            nc2=self.ugrid_history(nc_kwargs=dict(fn=nc2_fn,overwrite=True))
            # if no history output, no nc2.
            nc2 and nc2.close()
        if 1:
            nc_fn=os.path.join(self.base_path,'dwaq_map.nc')
            nc=self.ugrid_map(nc_kwargs=dict(fn=nc_fn,overwrite=True))
            # if no map output, no nc
            nc and nc.close()

    use_bloom=False
    def cmd_delwaq1(self):
        """
        Run delwaq1 preprocessor
        """
        if self.use_bloom:
            bloom_part="-eco {}".format(self.bloom_path)
        else:
            bloom_part=""

        cmd="{} -waq {} -p {}".format(self.delwaq1_path,
                                      bloom_part,
                                      self.proc_path)
        self.log.info("Running delwaq1:")
        self.log.info("  "+ cmd)

        t_start=time.time()
        try:
            ret=subprocess.check_output(cmd,shell=True,cwd=self.base_path,stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            self.log.error("problem running delwaq1")
            self.log.error("output: ")
            self.log.error("-----")
            self.log.error(exc.output)
            self.log.error("-----")
            raise WaqException("delwaq1 exited early.  check lst and lsp files")
        self.log.info("delwaq1 ran in %.2fs"%(time.time() - t_start))

        nerrors=nwarnings=-1
        for line in ret.decode().split("\n"):
            if 'Number of WARNINGS' in line:
                nwarnings=int(line.split()[-1])
            elif 'Number of ERRORS during input' in line:
                nerrors=int(line.split()[-1])
        if nerrors > 0 or nwarnings>0:
            print( ret )
            raise WaqException("delwaq1 found %d errors and %d warnings"%(nerrors,nwarnings))
        elif nerrors < 0 or nwarnings<0:
            print( ret)
            raise WaqException("Failed to find error/warning count")

    def cmd_delwaq2(self,output_filename=None):
        """
        Run delwaq2 (computation)
        """
        cmd="{} {}".format(self.delwaq2_path,self.name)
        if not output_filename:
            output_filename= os.path.join(self.base_path,'delwaq2.out')

        t_start=time.time()
        with open(output_filename,'wt') as fp_out:
            self.log.info("Running delwaq2 - might take a while...")
            self.log.info("  " + cmd)
            
            sim_time=(self.stop_time-self.start_time).total_seconds()
            tail=MonTail(os.path.join(self.base_path,self.name+".mon"),
                         log=self.log,sim_time_seconds=sim_time)
            try:
                try:
                    ret=subprocess.check_call(cmd,shell=True,cwd=self.base_path,stdout=fp_out,
                                              stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError as exc:
                    raise WaqException("delwaq2 exited with an error code - check %s"%output_filename)
            finally:
                tail.stop()

        self.log.info("delwaq2 ran in %.2fs"%(time.time()-t_start))

        # return value is not meaningful - have to scrape the output
        with open(output_filename,'rt') as fp:
            for line in fp:
                if 'Stopping the program' in line:
                    raise WaqException("Delwaq2 stopped early - check %s"%output_filename)
        self.log.info("Done")
            
    # Paths for Delft tools:
    @property
    def delft_path(self):
        # on linux probably one directory above bin directory
        if 'DELFT_SRC' not in os.environ:
            raise WaqException("Environment variable DELFT_SRC not defined")
        return os.environ['DELFT_SRC']
    @property
    def delft_bin(self):
        if 'DELFT_BIN' in os.environ:
            return os.environ['DELFT_BIN']
        return os.path.join(self.delft_path,'bin')
    @property
    def delwaq1_path(self):
        return os.path.join(self.delft_bin,'delwaq1')
    @property
    def delwaq2_path(self):
        return os.path.join(self.delft_bin,'delwaq2')
    @property
    def bloom_path(self):
        return os.path.join(self.delft_path,'engines_gpl/waq/default/bloom.spe')
    @property
    def original_bloominp_path(self):
        # this gets copied into the model run directory
        return os.path.join(self.delft_path,'engines_gpl/waq/default/bloominp.d09')
    @property
    def proc_path(self):
        return os.path.join(self.delft_path,'engines_gpl/waq/default/proc_def')


    # plot process diagrams
    def cmd_plot_process(self,run_name='dwaq'):
        """ Build a process diagram and save to file.  Sorry, you have no voice 
        in choosing the filename
        """
        pd = process_diagram.ProcDiagram(waq_dir=self.base_path)
        pd.render_dot()
    def cmd_view_process(self,run_name='dwaq'):
        """ Build a process diagram and display
        """
        pd = process_diagram.ProcDiagram(waq_dir=self.base_path)
        pd.view_dot()

class InpFile(object):
    """ define/access/generate the text input file for delwaq1 and delwaq2.
    """
    def __init__(self,scenario):
        self.log=logging.getLogger(self.__class__.__name__)
        self.scenario=scenario

    def default_filename(self):
        return os.path.join(self.scenario.base_path,
                            self.scenario.name+".inp")

    def write(self,fn=None):
        inp_fn=fn or self.default_filename()

        with open(inp_fn,'wt') as fp:
            fp.write(self.text())

    def text(self):
        return "".join( [self.text_header(),
                         self.text_block01(),
                         self.text_block02(),
                         self.text_block03(),
                         self.text_block04(),
                         self.text_block05(),
                         self.text_block06(),
                         self.text_block07(),
                         self.text_block08(),
                         self.text_block09(),
                         self.text_block10()])

    def text_header(self):
        header="""1000 132 ';'    ; width of input and output, comment
;
; Type of DELWAQ input file:
; DELWAQ_VERSION_4.91
; Option for printing the report: verbose
; PRINT_OUTPUT_OPTION_4

"""
        return header

    @property
    def n_substances(self):
        return self.scenario.n_substances
    @property
    def n_active_substances(self):
        return self.scenario.n_active_substances
    @property
    def n_inactive_substances(self):
        return self.scenario.n_inactive_substances

    def fmt_time0(self):
        return self.scenario.fmt_datetime(self.scenario.time0) # e.g. "1990.08.05 00:00:00"
    def fmt_scu(self):
        # no support yet for clock units < 1 second
        assert(self.scenario.scu.microseconds==0)
        # it's important that the output be exactly this wide!
        return "{:8}s".format(self.scenario.scu.seconds + 86400*self.scenario.scu.days)
    def fmt_substance_names(self):
        return "\n".join( ["    {:4}  '{}'".format(1+idx,s.name)
                           for idx,s in enumerate(self.scenario.substances.values())] )

    @property
    def integration_option(self):
        return self.scenario.integration_option

    @property
    def desc(self):
        return self.scenario.desc

    def text_block01(self):
        block01="""; first block: identification
'{self.desc[0]}'
'{self.desc[1]}'
'{self.desc[2]}'
'T0: {time0}  (scu={scu})'
;
; substances file: n/a
; hydrodynamic file: n/a
;
; areachar.dat: n/a
;
  {self.n_active_substances}  {self.n_inactive_substances}    ; number of active and inactive substances

; Index  Name
{substances}
;
#1 ; delimiter for the first block
"""
        return block01.format(self=self,
                              time0=self.fmt_time0(),scu=self.fmt_scu(),
                              substances=self.fmt_substance_names())

    @property
    def text_start_time(self):
        return self.scenario.fmt_datetime(self.scenario.start_time)
    @property
    def text_stop_time(self):
        return self.scenario.fmt_datetime(self.scenario.stop_time)

    @property
    def text_map_start_time(self):
        return self.scenario.fmt_datetime(self.scenario.map_start_time or 
                                          self.scenario.start_time)
    @property
    def text_map_stop_time(self):
        return self.scenario.fmt_datetime(self.scenario.map_stop_time or 
                                          self.scenario.stop_time)

    @property
    def text_hist_start_time(self):
        return self.scenario.fmt_datetime(self.scenario.hist_start_time or 
                                          self.scenario.start_time)
    @property
    def text_hist_stop_time(self):
        return self.scenario.fmt_datetime(self.scenario.hist_stop_time or 
                                          self.scenario.stop_time)
    @property
    def text_mon_start_time(self):
        return self.scenario.fmt_datetime(self.scenario.mon_start_time or 
                                          self.scenario.start_time)
    @property
    def text_mon_stop_time(self):
        return self.scenario.fmt_datetime(self.scenario.mon_stop_time or 
                                          self.scenario.stop_time)

    @property
    def time_step(self):
        return self.scenario.time_step
    @property
    def map_time_step(self):
        return self.scenario.map_time_step or self.scenario.time_step
    @property
    def hist_time_step(self):
        return self.scenario.hist_time_step or self.scenario.time_step
    @property
    def mon_time_step(self):
        return self.scenario.mon_time_step or self.scenario.time_step

    # start_time="1990/08/05-12:30:00"
    # stop_time ="1990/08/15-12:30:00"

    def text_monitor_areas(self):
        lines=["""
 1     ; monitoring points/areas used
 {n_points}   ; number of monitoring points/areas
""".format( n_points=len(self.scenario.monitor_areas) )]

        for name,segs in self.scenario.monitor_areas:
            # These can get quite long, so wrap the list of segments.
            # DWAQ can handle up to 1000 characters/line, but might as well
            # stop at 132 out of kindness.
            lines.append("'{}' {} {}".format(name,len(segs),
                                             textwrap.fill(" ".join(["%d"%(i+1) for i in segs]),
                                                           width=132)))

        return "\n".join(lines)

    def text_monitor_transects(self):
        n_transects=len(self.scenario.monitor_transects)

        if n_transects==0:
            return " 2     ; monitoring transects not used;\n"

        if len(self.scenario.monitor_areas)==0:
            # this is a real problem, though the code above for text_monitor_areas
            # has a kludge where it adds in a dummy monitoring area to avoid the issue
            raise Exception("DWAQ may not output transects when there are no monitor areas")

        lines=[" 1   ; monitoring transects used",
               " {n_transects} ; number of transects".format(n_transects=n_transects) ]
        for name,exchs in self.scenario.monitor_transects:
            # The 1 here is for reporting net flux.
            # split exchanges on multiple lines -- fortran may not appreciate
            # really long lines.
            lines.append("'{}' 1 {}".format(name,len(exchs)))
            lines+=["   %d"%i
                    for i in exchs]
        return "\n".join(lines)
    
    def text_monitor_start_stop(self):
        # not sure if the format matters - this was originally using dates like
        # 1990/08/05-12:30:00
        text="""; start time      stop time     time step 
 {self.text_mon_start_time}       {self.text_mon_stop_time}       {self.mon_time_step:08}      ; monitoring
 {self.text_map_start_time}       {self.text_stop_time}       {self.map_time_step:08}      ; map, dump
 {self.text_hist_start_time}       {self.text_hist_stop_time}       {self.hist_time_step:08}      ; history
"""
        return text.format(self=self)

    def text_block02(self):
        block02="""; 
; second block of model input (timers)
; 
; integration timers 
; 
 86400  'ddhhmmss' 'ddhhmmss' ; system clock in sec, aux in days
 {self.integration_option}    ; integration option
 {self.text_start_time}      ; start time 
 {self.text_stop_time}       ; stop time 
 0                  ; constant timestep 
 {self.time_step:07}      ; time step
;
{monitor_areas}
{monitor_transects}
{monitor_start_stop}
;
#2 ; delimiter for the second block
"""
        return block02.format(self=self,
                              monitor_areas=self.text_monitor_areas(),
                              monitor_transects=self.text_monitor_transects(),
                              monitor_start_stop=self.text_monitor_start_stop())
              
    @property
    def n_segments(self):
        return self.scenario.hydro.n_seg
    grid_layout=2 # docs suggest NONE would work, but seems to fail

    @property
    def multigrid_block(self):
        return self.scenario.multigrid_block

    @property
    def atr_filename(self):
        # updated to now include all of the attribute block
        return "com-{}.atr".format(self.scenario.name)

    #@property
    #def act_filename(self):
    #    return "com-{}.act".format(self.scenario.name)

    @property
    def vol_filename(self):
        return "com-{}.vol".format(self.scenario.name)

    @property
    def flo_filename(self):
        return "com-{}.flo".format(self.scenario.name)

    @property
    def are_filename(self):
        return "com-{}.are".format(self.scenario.name)

    @property
    def poi_filename(self):
        return "com-{}.poi".format(self.scenario.name)

    @property
    def len_filename(self):
        return "com-{}.len".format(self.scenario.name)

    def text_block03(self):
        block03="""; 
; third block of model input (grid layout)
 {self.n_segments}      ; number of segments
{self.multigrid_block}       ; multigrid block
 {self.grid_layout}        ; grid layout not used
;
; features
INCLUDE '{self.atr_filename}'  ; attributes file
;
; volumes
;
-2  ; first volume option
'{self.vol_filename}'  ; volumes file
;
#3 ; delimiter for the third block
"""
        return block03.format( self=self )

    @property
    def n_exch_x(self):
        return self.scenario.hydro.n_exch_x
    @property
    def n_exch_y(self):
        return self.scenario.hydro.n_exch_y
    @property
    def n_exch_z(self):
        return self.scenario.hydro.n_exch_z

    @property 
    def n_dispersions(self):
        # each dispersion array can have a name (<=20 characters)
        #  and if there are any, then we have to know which array
        #  if any goes with each substance
        return len(self.scenario.dispersions)

    @property
    def dispersions_declaration(self):
        """ the count and substance assignment for dispersion arrays
        """
        lines=[" {} ; dispersion arrays".format(len(self.scenario.dispersions))]
        if len(self.scenario.dispersions):
            subs=list( self.scenario.substances.keys() )[:self.scenario.n_active_substances]
            assignments=np.zeros(len(subs),'i4') # 1-based

            for ai,a in enumerate(self.scenario.dispersions.values()):
                lines.append(" '{}'".format(a.name))
                for subi,sub in enumerate(subs):
                    if a.matches(sub):
                        assignments[subi]=ai+1 # to 1-based
            lines.append( " ".join(["%d"%assign for assign in assignments])  + " ; assign to substances" )
        else:
            self.log.info("No dispersion arrays, will skip assignment to substances")
        return "\n".join(lines)

    @property
    def dispersions_definition(self):
        if len(self.scenario.dispersions)==0:
            return ""
        else:
            lines=[';Data option',
                   '1 ; information is constant and provided without defaults']

            hydro=self.scenario.hydro

            # add x direction:
            lines.append("1.0 ; scale factor for x")
            disps=self.scenario.dispersions.values()

            for exch_i in range(hydro.n_exch_x):
                vals=[disp.data[exch_i] for disp in disps]
                lines.append( " ".join(["%.3e"%v for v in vals]) + "; from each array" )

            assert hydro.n_exch_y==0 # not implemented
                
            lines.append("1.0 ; scale factor for z")

            for exch_i in range(hydro.n_exch_x+hydro.n_exch_y,hydro.n_exch):
                vals=[disp.data[exch_i] for disp in disps]
                lines.append( " ".join(["%.3e"%v for v in vals]) + "; from each array" )
                
            # Write them out to a separate text file
            disp_filename='dispersions.dsp'
            with open(os.path.join(self.scenario.base_path,disp_filename),'wt') as fp:
                fp.write("\n".join(lines))
            return "INCLUDE '{}'".format(disp_filename)

    @property
    def base_x_dispersion(self):
        return self.scenario.base_x_dispersion
    @property
    def base_y_dispersion(self):
        return self.scenario.base_y_dispersion
    @property
    def base_z_dispersion(self):
        return self.scenario.base_z_dispersion

    def text_block04(self):
        block04="""; 
; fourth block of model input (transport)
 {self.n_exch_x}  ; exchanges in direction 1
 {self.n_exch_y}  ; exchanges in direction 2
 {self.n_exch_z}  ; exchanges in direction 3
; 
 {self.dispersions_declaration}
 0  ; velocity arrays
; 
 1  ; first form is used for input 
 0  ; exchange pointer option
'{self.poi_filename}'  ; pointers file
; 
 1  ; first dispersion option nr - these constants will be added in.
 1.0 1.0 1.0   ; scale factors in 3 directions
 {self.base_x_dispersion} {self.base_y_dispersion} {self.base_z_dispersion} ; dispersion in x,y,z directions
; this is where the GUI puts an INCLUDE 'test.dsp'
{self.dispersions_definition}
; 
 -2  ; first area option
'{self.are_filename}'  ; area file
; 
 -2  ; first flow option
'{self.flo_filename}'  ; flow file
; 
; Maybe this is where explicit dispersion arrays go??
; No explicit velocity arrays
; 
  1  ; length vary
 0   ; length option
'{self.len_filename}'  ; length file
;
#4 ; delimiter for the fourth block
"""
        return block04.format(self=self)

        # including explicit dispersion arrays:
        # this page: http://oss.deltares.nl/web/delft3d/delwaq/-/message_boards/view_message/583767;jsessionid=3C8F18A0BB9B95EE1FFE77F72764DD77
        # shows a text format
        # the GUI puts

    @property
    def text_boundary_defs(self):
        # a triple of strings for each boundary exchange
        # first is id, must be unique in 20 characters
        # second is name, freeform
        # third is type, will be matched with first 20 characters
        # to group boundaries together.

        lines=[]
        for bdry in self.scenario.hydro.boundary_defs():
            lines.append("'{}' '{}' '{}'".format( bdry['id'].decode(),
                                                  bdry['name'].decode(),
                                                  bdry['type'].decode() ) )
        return "\n".join(lines)
    
    @property
    def n_boundaries(self):
        return self.scenario.hydro.n_boundaries

    @property
    def text_overridings(self):
        lines=[ "{:5}    ; Number of overridings".format(self.n_boundaries) ]
        for bi in range(self.n_boundaries):
            lines.append( "  {:9}     00000000000000   ; Left-right 1".format(bi+1) )
        return "\n".join(lines)

    # Boundary condition definitions:

    @property
    def text_thatcher_harleman_lags(self):
        # not ready to build in explicit handling of this, so for
        # now delegate to Scenario (so at least customization is
        # centralized there)
        return self.scenario.text_thatcher_harleman_lags()

    @property
    def text_bc_items(self):
        lines=[]
        for bc in self.scenario.bcs:
            lines.append( bc.text() )
        return "\n".join(lines)

    def text_block05(self):
        block05="""; 
; fifth block of model input (boundary condition)
{self.text_boundary_defs}
{self.text_thatcher_harleman_lags}
{self.text_bc_items}
;
 #5 ; delimiter for the fifth block
"""
        return block05.format(self=self)

    @property
    def n_discharges(self):
        return len(self.scenario.discharges)

    @property
    def text_discharge_names(self):
        lines=[]
        for disch in self.scenario.discharges:
            # something like that - 
            lines.append( disch.text() )
        return "\n".join(lines)

    @property
    def text_discharge_items(self):
        lines=[]
        for load in self.scenario.loads:
            lines.append( load.text() )
        return "\n".join(lines)

    @property
    def par_filename(self):
        return self.scenario.name+".par"

    @property
    def vdf_filename(self):
        return "com-{}.vdf".format(self.scenario.name)

    def text_block06(self):
        block06="""; 
; sixth block of model input (discharges, withdrawals, waste loads)
   {self.n_discharges} ; number of waste loads/continuous releases
{self.text_discharge_names}
{self.text_discharge_items}
;
 #6 ; delimiter for the sixth block
""".format(self=self)
        return block06

    def text_block07(self):
        lines=['; seventh block of model input (process parameters)']

        for param in self.scenario.parameters.values():
            lines.append( param.text(write_supporting=True) )
        for param in self.scenario.hydro_parameters.values():
            # hydro.write() takes care of writing its own parameters
            lines.append( param.text(write_supporting=False) )

        lines.append("#7 ; delimiter for the seventh block")
        return "\n".join(lines)

    def text_block08(self):
        # unclear how to add spatially varying initial condition
        # in new style.
        return self.text_block08_old()

    def text_block08_new(self):
        """ new style initial conditions - NOT USED """

        defaults="\n".join([" {:e} ; {}".format(s.initial.default,s.name)
                            for s in self.scenario.substances.values() ])
        lines=["; ",
               "; eighth block of model input (initial conditions) ",
               " MASS/M2 ; unit for inactive substances",
               " INITIALS ",
               # list of substances
               " ".join( [" {} ".format(s)
                          for s in self.scenario.substances.values()] )]
        # pick up here.
        raise Exception("New style initial condition code not implemented yet")

        return "\n".join(lines)
        
    def text_block08_old(self):
        """ old-style initial conditions.  note this is old-style, not
        old code.  This is the version currently used!
        """
        lines=["; ",
               "; eighth block of model input (initial conditions) ",
               " MASS/M2 ; unit for inactive substances",
               " 1 ; initial conditions follow"]

        # are any initial conditions spatially varying?
        # if so, then skip defaults and specify all substances, everywhere
        for s in self.scenario.substances.values():
            if s.initial.seg_values is not None:
                lines+=self.text_ic_old_spatially_varying()
                break
        else:
            # otherwise, just give defaults:
            defaults="\n".join([" {:e} ; {}".format(s.initial.default,s.name)
                            for s in self.scenario.substances.values() ])
            lines+=[ " 2 ; all values with default",
                     "{self.n_substances}*1.0 ; scale factors".format(self=self),
                     defaults,
                     " 0  ; overridings"]

        lines+=[ ";",
                 " #8 ; delimiter for the eighth block"]

        return "\n".join(lines)

    def text_ic_old_spatially_varying(self):
        """ return lines for initial conditions when they are spatially varying
        """
        # use transpose, so that it's easy to write defaults when we have them,
        # and spatially varying when needed
        subs=self.scenario.substances.values()

        lines=["TRANSPOSE",
               "1 ; without defaults",
               "1.0 ; scaling for all substances"]

        # "{}*1.0 ; no scaling".format(len(subs))] # doesn't work!

        # documentation suggests that all scale factors come at the beginning,
        # and that it includes one scale per substance. error output suggests
        # that with TRANSPOSE, we get only one scale factor total.

        for s in subs:
            if s.initial.seg_values is not None:
                lines.append(" ; spatially varying for {}".format(s.name) )
                lines += [" %f"%val for val in s.initial.seg_values]
            else:
                lines.append( " %d*%f ; default for %s"%(self.scenario.hydro.n_seg,
                                                         s.initial.default,s.name) )
        return lines

    def text_block09(self):
        lines=[";",
               " ; ninth block of model input (specification of output)",
               "1 ; output information in this file" ]
        MONITOR='monitor'
        GRID='grid dump'
        HIS='history'
        MAP='map'

        outputs=[self.scenario.mon_output,
                 self.scenario.grid_output,
                 self.scenario.hist_output,
                 self.scenario.map_output]
        for spec,output_type in zip(outputs,
                                    [MONITOR,GRID,HIS,MAP]):
            spec=list(np.unique(spec))
            if output_type in [MONITOR,HIS]:
                weighing=" ' '"
            else:
                weighing=""
            vnames=["  '{name}' {weighing}".format(name=name,weighing=weighing)
                    for name in spec
                    if name!=DEFAULT]
            if len(spec)==0:
                lines.append('0 ; no output for {}'.format(output_type))
            elif DEFAULT in spec:
                if vnames:
                    lines.append(" 2 ; all substances and extra output, {}".format(output_type))
                    lines.append(" {} ; number of extra".format(len(vnames)))
                    lines+=vnames
                else:
                    lines.append('  1 ; only default, {} output'.format(output_type))
            else:
                lines.append("  3 ; only extras, {} output".format(output_type))
                lines.append("{} ; number of extra".format(len(vnames)))
                lines+=vnames
        lines += ["  1 ; binary history file on",
                  "  0 ; binary map     file on",
                  "  1 ; nefis  history file on",
                  "  1 ; nefis  map     file on",
                  "; ",
                  " #9 ; delimiter for the ninth block"]

        return "\n".join(lines)

    def text_block10(self):
        lines=[";",
               "; Statistical output - if any",
               "; INCLUDE 'tut_fti_waq.stt' ",
               "; ",
               " #10 ; delimiter for the tenth block "]
        return "\n".join(lines)
