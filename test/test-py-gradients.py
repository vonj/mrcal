#!/usr/bin/python3

r'''Tests gradients reported by the python code

This is conceptually similar to test-gradients.py, but here I validate the
python code path

'''

import sys
import numpy as np
import numpysane as nps
import os

testdir = os.path.dirname(os.path.realpath(__file__))

# I import the LOCAL mrcal since that's what I'm testing
sys.path[:0] = f"{testdir}/..",
import mrcal
import testutils


intrinsics = \
    ( ('LENSMODEL_PINHOLE',
       np.array((1512., 1112, 500., 333.))),
      ('LENSMODEL_OPENCV4',
       np.array((1512., 1112, 500., 333.,
                 -0.012, 0.035, -0.001, 0.002))),
      ('LENSMODEL_CAHVOR',
       np.array((4842.918,4842.771,1970.528,1085.302,
                 -0.001, 0.002, -0.637, -0.002, 0.016))),
      ('LENSMODEL_SPLINED_STEREOGRAPHIC_3_11_8_200',
       np.array([ 1900, 1800, 1499.5,999.5,
                  2.017284705,1.242204557,2.053514381,1.214368063,2.0379067,1.212609628,
                  2.033278227,1.183689487,2.040018023,1.188554431,2.069146825,1.196304649,
                  2.085708658,1.186478238,2.065787617,1.163377825,2.086372192,1.138856716,
                  2.131609155,1.125678279,2.128812604,1.120525061,2.00841491,1.21864154,
                  2.024522768,1.239588759,2.034947935,1.19814079,2.065474055,1.19897294,
                  2.044562395,1.200557321,2.087714092,1.160440038,2.086478691,1.151822407,
                  2.112862582,1.147567288,2.101575718,1.146312256,2.10056469,1.157015327,
                  2.113488262,1.111679758,2.019837901,1.244168216,2.025847768,1.215633807,
                  2.041980956,1.205751212,2.075077056,1.199787561,2.070877831,1.203261678,
                  2.067244278,1.184705736,2.082225077,1.185558149,2.091519961,1.17501817,
                  2.120258866,1.137775228,2.120020747,1.152409316,2.121870228,1.113069319,
                  2.043650555,1.247757041,2.019661062,1.230723629,2.067917203,1.209753396,
                  2.035034141,1.219514335,2.045350268,1.178474255,2.046346049,1.169372592,
                  2.097839998,1.194836758,2.112724938,1.172186377,2.110996386,1.154899043,
                  2.128456883,1.133228404,2.122513384,1.131717886,2.044279196,1.233288366,
                  2.023197297,1.230118703,2.06707694,1.199998862,2.044147271,1.191607451,
                  2.058590053,1.1677808,2.081593501,1.182074581,2.08663053,1.159156329,
                  2.084329086,1.157727374,2.073666528,1.151261965,2.114290905,1.144710519,
                  2.138600912,1.119405248,2.016299528,1.206147494,2.029434175,1.211507857,
                  2.057936091,1.19801196,2.035691392,1.174035359,2.084718618,1.203604729,
                  2.085910021,1.158385222,2.080800068,1.150199852,2.087991586,1.162019581,
                  2.094754507,1.151061493,2.115144642,1.154299799,2.107014195,1.127608146,
                  2.005632475,1.238607328,2.02033157,1.202101384,2.061021703,1.214868271,
                  2.043015135,1.211903685,2.05291186,1.188092787,2.09486724,1.179277314,
                  2.078230124,1.186273023,2.077743945,1.148028845,2.081634186,1.131207467,
                  2.112936851,1.126412871,2.113220553,1.114991063,2.017901873,1.244588667,
                  2.051238803,1.201855728,2.043256406,1.216674722,2.035286046,1.178380907,
                  2.08028318,1.178783085,2.051214271,1.173560417,2.059298121,1.182414688,
                  2.094607679,1.177960959,2.086998287,1.147371259,2.12029442,1.138197348,
                  2.138994213, 1.114846113,])))

# a few points, some wide, some not. None behind the camera
p = np.array(((1.0, 2.0, 10.0),
              (-1.1, 0.3, 1.0),
              (-0.9, -1.5, 1.0)))

delta = 1e-6

for i in intrinsics:

    q,dq_dp,dq_di = mrcal.project(p, *i, get_gradients=True)

    Nintrinsics = mrcal.getNlensParams(i[0])
    testutils.confirm_equal(dq_di.shape[-1], Nintrinsics,
                            msg=f"{i[0]}: Nintrinsics match for {i[0]}")
    if Nintrinsics != dq_di.shape[-1]:
        continue

    for ivar in range(dq_dp.shape[-1]):

        # center differences
        p1 = p.copy()
        p1[..., ivar] = p[..., ivar] - delta/2
        q1 = mrcal.project(p1, *i, get_gradients=False)
        p1[..., ivar] += delta
        q2 = mrcal.project(p1, *i, get_gradients=False)

        dq_dp_observed = (q2 - q1) / delta
        dq_dp_reported = dq_dp[..., ivar]

        testutils.confirm_equal(dq_dp_reported, dq_dp_observed,
                                eps=1e-5,
                                msg=f"{i[0]}: dq_dp matches for var {ivar}")

    for ivar in range(dq_di.shape[-1]):

        # center differences
        i1 = i[1].copy()
        i1[..., ivar] = i[1][..., ivar] - delta/2
        q1 = mrcal.project(p, i[0], i1, get_gradients=False)
        i1[..., ivar] += delta
        q2 = mrcal.project(p, i[0], i1, get_gradients=False)

        dq_di_observed = (q2 - q1) / delta
        dq_di_reported = dq_di[..., ivar]

        testutils.confirm_equal(dq_di_reported, dq_di_observed,
                                eps=1e-5,
                                msg=f"{i[0]}: dq_di matches for var {ivar}")
testutils.finish()
