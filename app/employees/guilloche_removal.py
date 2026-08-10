"""FFT-based guilloche pattern removal for Malaysian MyKad OCR.

Guilloche patterns are periodic background lines used as security features.
They create frequency-domain peaks that interfere with OCR. This module
suppresses those peaks via FFT filtering, preserving text while removing
the security pattern.
"""
import numpy as np
from PIL import Image


def remove_guilloche(pil_img):
    """Remove periodic guilloche patterns via FFT frequency filtering.

    Steps:
    1. Convert to grayscale float array
    2. FFT → frequency domain
    3. Identify & suppress periodic peaks (guilloche frequencies)
    4. Inverse FFT → cleaned image
    """
    gray = pil_img.convert('L')
    arr = np.array(gray, dtype=np.float64)

    rows, cols = arr.shape
    center_y, center_x = rows // 2, cols // 2

    f = np.fft.fft2(arr)
    fshift = np.fft.fftshift(f)
    magnitude = np.abs(fshift)

    # Compute local mean of magnitude spectrum to find periodic peaks
    block = max(11, min(rows, cols) // 20)
    if block % 2 == 0:
        block += 1
    from scipy.ndimage import uniform_filter
    local_mean = uniform_filter(magnitude, size=block)

    # Peaks are pixels significantly above local average
    # Guilloche peaks are strong and narrow
    peak_mask = magnitude > local_mean * 1.8
    # Preserve DC (center) and very low frequencies (text)
    y_grid, x_grid = np.ogrid[:rows, :cols]
    dist_from_center = np.sqrt((y_grid - center_y)**2 + (x_grid - center_x)**2)
    low_freq_mask = dist_from_center < max(rows, cols) * 0.08
    peak_mask[low_freq_mask] = False

    # Suppress peaks by replacing with local mean
    fshift[peak_mask] = local_mean[peak_mask] * np.exp(1j * np.angle(fshift[peak_mask]))

    f_ishift = np.fft.ifftshift(fshift)
    img_back = np.fft.ifft2(f_ishift)
    cleaned = np.abs(img_back).clip(0, 255).astype(np.uint8)

    return Image.fromarray(cleaned)
