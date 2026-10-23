//! Whether the capture times of a sequence make a constant-frame-rate video.
//!
//! Every image becomes one frame of the video, whatever the timing: no frame
//! is dropped or repeated. A constant-frame-rate video puts frame *k* at *k*
//! frame periods from the first one, so it is used only when that loses no
//! timing: when every capture time read back from it is within half a
//! millisecond, as close as a variable-frame-rate video keeps them (FFmpeg's
//! Matroska muxer stores timestamps in milliseconds). Otherwise each frame
//! keeps its own capture time as timestamp: a variable frame rate.
//!
//! The server decides with [`frame_timing`] once every image of an upload is
//! in: the client only sends the images and their capture times.

use std::fmt;

use serde::{Deserialize, Serialize};

/// The unit of the video's timestamps: FFmpeg's Matroska muxer stores them in
/// milliseconds.
pub const TIMESTAMP_UNIT_NS: i64 = 1_000_000;

/// The frame rate tried first: the sequence was shot at 29.97 fps.
pub const NOMINAL_FRAME_RATE: FrameRate = FrameRate { num: 30000, den: 1001 };

/// Largest denominator of a frame rate estimated from the capture times:
/// 1001 is the one of the NTSC rates (24000/1001, 30000/1001, 60000/1001).
const MAX_DENOMINATOR: i128 = 1001;

/// A frame rate, in frames per `den` seconds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameRate {
    pub num: i32,
    pub den: i32,
}

impl FrameRate {
    /// The timestamp of frame `index` in a constant-frame-rate video, in the
    /// video's unit ([`TIMESTAMP_UNIT_NS`]): `index` frame periods, rounded to
    /// the nearest unit with halves up, as the muxer rounds.
    fn stored_ns(self, index: u64) -> i64 {
        let units_per_second = 1_000_000_000 / i128::from(TIMESTAMP_UNIT_NS);
        let exact = i128::from(index) * i128::from(self.den) * units_per_second;
        let num = i128::from(self.num);
        ((2 * exact + num) / (2 * num)) as i64 * TIMESTAMP_UNIT_NS
    }
}

impl fmt::Display for FrameRate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.num, self.den)
    }
}

/// How the frames of a video are timed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "lowercase")]
pub enum FrameTiming {
    /// Frame *k* at *k* periods of `frame_rate` from the first frame.
    Constant { frame_rate: FrameRate },
    /// Each frame at its own capture time.
    #[default]
    Variable,
}

impl fmt::Display for FrameTiming {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FrameTiming::Constant { frame_rate } => write!(f, "constant frame rate {frame_rate}"),
            FrameTiming::Variable => f.write_str("variable frame rate"),
        }
    }
}

/// Whether frame `index`, captured `offset_ns` after the first frame, reads
/// back within half a millisecond of its capture time from a video at the
/// constant `frame_rate`.
fn on_grid(frame_rate: FrameRate, index: u64, offset_ns: i64) -> bool {
    (offset_ns - frame_rate.stored_ns(index)).abs() <= TIMESTAMP_UNIT_NS / 2
}

/// How to time a video of images captured at `timestamps_ns`, in upload
/// order: at a constant frame rate if one keeps every capture time (see
/// [`on_grid`]), trying [`NOMINAL_FRAME_RATE`] first and then the rate of
/// the first and last capture times; otherwise at a variable frame rate. A
/// single image is at the nominal rate.
pub fn frame_timing(timestamps_ns: &[i64]) -> FrameTiming {
    let (&first, &last) = match timestamps_ns {
        [first, .., last] => (first, last),
        _ => {
            return FrameTiming::Constant {
                frame_rate: NOMINAL_FRAME_RATE,
            };
        }
    };
    let fits = |frame_rate: FrameRate| {
        timestamps_ns
            .iter()
            .enumerate()
            .all(|(index, t)| on_grid(frame_rate, index as u64, t - first))
    };
    let frames = timestamps_ns.len() as i128 - 1;
    let span = i128::from(last) - i128::from(first);
    let estimated = (span > 0)
        .then(|| limit_denominator(frames * 1_000_000_000, span, MAX_DENOMINATOR))
        .and_then(|(num, den)| Some(FrameRate { num: num.try_into().ok()?, den: den.try_into().ok()? }))
        .filter(|rate| rate.num > 0);
    [Some(NOMINAL_FRAME_RATE), estimated]
        .into_iter()
        .flatten()
        .find(|&rate| fits(rate))
        .map_or(FrameTiming::Variable, |frame_rate| FrameTiming::Constant { frame_rate })
}

/// The fraction closest to `num / den` with a denominator of at most
/// `max_den`, reduced (Python's `Fraction.limit_denominator`). `num` and
/// `den` are positive.
fn limit_denominator(num: i128, den: i128, max_den: i128) -> (i128, i128) {
    let gcd = |mut a: i128, mut b: i128| {
        while b != 0 {
            (a, b) = (b, a % b);
        }
        a
    };
    let g = gcd(num, den);
    let (num, den) = (num / g, den / g);
    if den <= max_den {
        return (num, den);
    }
    // The convergents of the continued fraction, until the next one's
    // denominator is too large.
    let (mut p0, mut q0, mut p1, mut q1) = (0, 1, 1, 0);
    let (mut n, mut d) = (num, den);
    while d != 0 {
        let a = n / d;
        let q2 = q0 + a * q1;
        if q2 > max_den {
            break;
        }
        (p0, q0, p1, q1) = (p1, q1, p0 + a * p1, q2);
        (n, d) = (d, n - a * d);
    }
    // The last convergent, or the best semiconvergent before the next one.
    let k = (max_den - q0) / q1;
    let semi = (p0 + k * p1, q0 + k * q1);
    let distance = |(p, q): (i128, i128)| ((p * den - num * q).abs(), q);
    let ((e1, q_semi), (e2, q_conv)) = (distance(semi), distance((p1, q1)));
    // e / (q * den) compared without dividing.
    if e2 * q_semi <= e1 * q_conv { (p1, q1) } else { semi }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{images::read_timestamps, workspace_dir};

    fn timing_of(scenario: &str) -> FrameTiming {
        let csv = workspace_dir().join(format!("datasets/{scenario}/batch_060/timestamps.csv"));
        let times: Vec<i64> = read_timestamps(&csv).unwrap().iter().map(|i| i.timestamp_ns).collect();
        frame_timing(&times)
    }

    #[test]
    fn regular_timing_is_constant() {
        assert_eq!(
            timing_of("regular"),
            FrameTiming::Constant {
                frame_rate: NOMINAL_FRAME_RATE
            }
        );
    }

    #[test]
    fn irregular_timing_is_variable() {
        for scenario in ["jitter", "frame-drops", "rate-change"] {
            assert_eq!(timing_of(scenario), FrameTiming::Variable, "{scenario}");
        }
    }

    #[test]
    fn other_constant_rates_are_found() {
        // 25 fps, exactly 40 ms apart.
        let times: Vec<i64> = (0..100).map(|i| 1_000_000_000_000 + i * 40_000_000).collect();
        assert_eq!(
            frame_timing(&times),
            FrameTiming::Constant {
                frame_rate: FrameRate { num: 25, den: 1 }
            }
        );
    }

    #[test]
    fn one_image_is_constant() {
        assert!(matches!(frame_timing(&[42]), FrameTiming::Constant { .. }));
    }

    #[test]
    fn off_grid_by_more_than_half_a_millisecond_is_variable() {
        let mut times: Vec<i64> = (0..30).map(|i| NOMINAL_FRAME_RATE.stored_ns(i)).collect();
        times[10] += 600_000;
        assert_eq!(frame_timing(&times), FrameTiming::Variable);
        times[10] -= 200_000; // 0.4 ms off: still read back within 0.5 ms
        assert!(matches!(frame_timing(&times), FrameTiming::Constant { .. }));
    }

    #[test]
    fn ntsc_rates_are_recovered() {
        assert_eq!(limit_denominator(59_000_000_000, 1_968_633_333, 1001), (30000, 1001));
        assert_eq!(limit_denominator(1, 3, 1001), (1, 3));
    }
}
