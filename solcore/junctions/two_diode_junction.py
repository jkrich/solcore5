from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Type, Mapping, NamedTuple
from collections import ChainMap
import numpy as np
import xarray as xr
from scipy.optimize import root

from ..junction_base import JunctionBase
from ..light_source_base import LightSource
from ..constants import q, kb


class TwoDiodeData(NamedTuple):
    """Object storing the parameters of a 2 diode equation.

    t (float): Junction temperature.
    j01 (float): Saturation current 1.
    j02 (float): Saturation current 2.
    n1 (float): Ideality factor 1.
    n2 (float): Ideality factor 2.
    rs (float): Series resistance.
    rsh (float): Shunt resistance.
    js (float, optional): Short circuit current. If provided, it will be used in the IV
        calculations instead of calculating it from the absorption and the spectrum.
    """

    t: float = 297
    j01: float = 1e-6
    j02: float = 0.0
    n1: float = 1.0
    n2: float = 2.0
    rs: float = 0.0
    rsh: float = 1e14
    jsc: Optional[float] = None


@dataclass(frozen=True)
class TwoDiodeJunction(JunctionBase):
    """Junction class for the two diode model.

    data (TwoDiodeData): Object storing the parameters of a 2 diode equation.
    params (Mapping): Other parameters with physical information, possibly used during
        the construction of the TwoDiodeData object.
    options (Mapping): Options to pass to the iv calculator - used if series resistance
        is not None.
    """

    data: TwoDiodeData = TwoDiodeData()
    params: Optional[Mapping] = None
    options: Optional[Mapping] = None

    @classmethod
    def from_reference(
        cls,
        band_gap: float,
        t: float,
        data: TwoDiodeData = TwoDiodeData(),
        params: Optional[Mapping] = None,
        options: Optional[Mapping] = None,
    ):
        """Initialises a TwoDiodeJunction out of TwoDiodeData at different temperature.

        We want the junction to be defined at a certain temperature t, but we know
        the parameters at a different temperature, contained in the data object.
        Assuming the temperatures are not too different, we can estimate a new set of
        parameters using the bandgap of the junction.

        Args:
            band_gap: Bandgap associated to the junction.
            t: Temperature of interest.
            data (TwoDiodeData): Object storing the parameters of a 2 diode equation at
                a reference temperature.
            params (Mapping): Other parameters with physical information, possibly used
                during the construction of the TwoDiodeData object.
            options (Mapping): Options to pass to the iv calculator - used if series
                resistance is not None.

        Returns:
            New instance of a TwoDiodeJunction
        """
        j01 = (
            data.j01
            * (t / data.t) ** 3
            * np.exp(-q * band_gap / (data.n1 * kb) * (1 / t - 1 / data.t))
        )
        j02 = (
            data.j02
            * (t / data.t) ** (5.0 / 3.0)
            * np.exp(-q * band_gap / (data.n2 * kb) * (1 / t - 1 / data.t))
        )
        parameters = ChainMap(
            {"band_gap": band_gap, "t_ref": data.t},
            params if params is not None else {},
        )
        return cls(data._replace(t=t, j01=j01, j02=j02), parameters, options)

    def solve_iv(
        self,
        voltage: np.ndarray,
        absorption: Optional[xr.DataArray] = None,
        light_source: Optional[Type[LightSource]] = None,
    ) -> xr.Dataset:
        """Calculates the IV curve of the junction.

        If absorption is provided, then light_source must also be provided and the
        light IV curve should be calculated instead. In this case, parameters like
        Voc, Isc, fill factor, etc. are also calculated.

        Args:
            voltage (np.ndarray): Array of voltages at which to calculate the IV curve.
            absorption (xr.DataArray, optional): Array with the fraction of absorbed
                light as a function of 'wavelength' and 'position'.
            light_source (LightSource, optional): Light source to be used in the case of
                light IV.

        Returns:
            A xr.Dataset with the output of the calculation. Contains a 'current'
            DataArray giving the current in amps as a function of the input 'voltage'.
            If light IV is calculated, the curve parameters (Voc, Isc, FF, Vmpp, Impp,
            Pmpp and eta) are provided as attributes of the Dataset.
        """
        jsc = (
            self.data.jsc
            if self.data.jsc is not None
            else self._get_jsc(absorption, light_source)
        )

        i = iv2diode(voltage, self.data._replace(jsc=jsc), **self.options)

        current = xr.DataArray(i, dims=["voltage"], coords={"voltage": voltage})
        parameters = {} if jsc == 0.0 else self.iv_parameters(voltage, current.values)
        return xr.Dataset({"current": current}, attrs=parameters)

    def solve_qe(
        self, absorption: xr.DataArray, light_source: Optional[Type[LightSource]] = None
    ) -> xr.Dataset:
        """Calculates the external and internal quantum efficiency of the junction.

        Args:
            absorption (xr.DataArray, optional): Array with the fraction of absorbed
                light as a function of 'wavelength' and 'position'.
            light_source (LightSource, optional): Light source to use in the
                calculation. Ignored.

        Returns:
            A xr.Dataset with the 'eqe' and 'iqe' DataArrays as a function of
            'wavelength'.
        """
        eqe = absorption.integrate("position")
        iqe = xr.ones_like(eqe)

        return xr.Dataset({"eqe": eqe, "iqe": iqe})

    def solve_equilibrium(self):
        raise NotImplementedError

    def solve_short_circuit(
        self, absorption: xr.DataArray, light_source: Type[LightSource]
    ) -> xr.Dataset:
        raise NotImplementedError

    def _get_jsc(
        self,
        absorption: Optional[xr.DataArray] = None,
        light_source: Optional[Type[LightSource]] = None,
    ) -> float:
        """Calculates the short circuit current out of the absorption.

        Args:
            absorption (xr.DataArray, optional): Array with the fraction of absorbed
                light as a function of 'wavelength' and 'position'.
            light_source (LightSource, optional): Light source to use in the
                calculation.

        Returns:
            The short circuit current.
        """
        if absorption is None or light_source is None:
            return 0.0

        sp = xr.DataArray(
            light_source.spectrum(
                absorption.wavelength, output_units="photon_flux_per_m"
            ),
            dims=["wavelength"],
            coords={"wavelength": absorption.wavelength},
        )
        return q * (self.solve_qe(absorption).eqe * sp).integrate("wavelength")


def iv_no_rs(v: np.ndarray, data: TwoDiodeData = TwoDiodeData()) -> np.ndarray:
    """Calculates the current at the chosen voltages using the given parameters.

    Args:
        v (np.ndarray): Voltages at which to calculate the currents.
        data (TwoDiodeData): Object storing the parameters of a 2 diode equation.

    Returns:
        Numpy array of the same length as the voltages with the currents.
    """
    return (
        data.j01 * (np.exp(q * v / (data.n1 * kb * data.t)) - 1)
        + data.j02 * (np.exp(q * v / (data.n2 * kb * data.t)) - 1)
        + v / data.rsh
        - data.jsc
    )


def iv2diode(
    v: np.ndarray, data: TwoDiodeData = TwoDiodeData(), **kwargs,
) -> np.ndarray:
    """Calculates the current using the 2-diodes equation.

    If series resistance is zero, it just return the result of replacing all the
    parameters in the 2 diode equation. Otherwise, scipy.optimize.root is used to
    numerically solve the transcendental equation.
    TODO: Not working for rs != 0, yet!
    Args:
        v (np.ndarray): Voltages at which to calculate the currents.
        data (TwoDiodeData): Object storing the parameters of a 2 diode equation.
        kwargs: Options to be passed to the root finding algorithm.

    Returns:
        Numpy array of the same length as the voltages with the currents.
    """
    current = iv_no_rs(v, data)
    if data.rs == 0.0:
        return current

    def fun(j):
        return j - iv_no_rs(v - data.rs * j, data)

    jguess = np.clip(current, np.min(v) / data.rs, np.max(v) / data.rs)
    sol = root(fun, jguess, **kwargs)
    if not sol.success:
        msg = f"The IV calculation failed to converge. Solver info:\n{sol}"
        raise RuntimeError(msg)

    return sol.x
