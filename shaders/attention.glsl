// Blink for Ghostty: amber wash over a pane whose agent waits for the user.
// The hook drives it through palette slot 255 (OSC 4), separately per pane:
// #01b1XX means "waiting", with XX the pulse intensity (00-ff). The hook
// rewrites XX ~15 times per second, so Ghostty can keep
// `custom-shader-animation = false`: no render loop while nothing waits.
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec4 original = texture(iChannel0, fragCoord / iResolution.xy);
    vec3 marker = iPalette[255] * 255.0;
    bool waiting = abs(marker.r - 1.0) < 0.5 && abs(marker.g - 177.0) < 0.5;

    // Exact passthrough while idle, and in the pane you are looking at.
    if (!waiting || iFocus > 0) {
        fragColor = original;
        return;
    }

    // Smooth amber wash over EVERY pixel, including text and explicit RGB
    // backgrounds. No borders, no displacement, no hard flash.
    float amount = 0.04 + 0.23 * marker.b / 255.0;
    vec3 amber = vec3(1.0, 0.57, 0.17);
    fragColor = vec4(mix(original.rgb, amber * original.a, amount), original.a);
}
