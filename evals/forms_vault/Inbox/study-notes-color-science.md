Notes on color science fundamentals, from the grading course.

Gamma encoding exists because human brightness perception is nonlinear: we are
far more sensitive to changes in shadows than in highlights. A power function
of roughly 2.2 spreads the available code values to match that sensitivity.

Rec.709 and sRGB share primaries but differ in transfer function. Rec.709
cameras apply a knee above 80 IRE; sRGB uses a linear segment near black to
avoid an infinite derivative at zero.

A LUT is a sampled function, not a formula: a 33-point cube interpolates
between samples, which is why extreme values can band after a strong grade.
