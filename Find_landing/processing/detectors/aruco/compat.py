"""OpenCV ArUco — ArucoDetector (4.7+) hoặc detectMarkers legacy (4.6)."""

from __future__ import annotations

import cv2


class _LegacyArucoDetector:
    def __init__(self, dictionary, params):
        self._dictionary = dictionary
        self._params = params

    def detectMarkers(self, gray):
        return cv2.aruco.detectMarkers(gray, self._dictionary, parameters=self._params)


def create_aruco_detector(dictionary):
    params = cv2.aruco.DetectorParameters()
    params.adaptiveThreshWinSizeMin = 5
    params.adaptiveThreshWinSizeMax = 35
    params.adaptiveThreshWinSizeStep = 5
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    if hasattr(cv2.aruco, "ArucoDetector"):
        return cv2.aruco.ArucoDetector(dictionary, params)
    return _LegacyArucoDetector(dictionary, params)
