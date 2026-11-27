package chat.wire;

import java.util.Objects;

/** One unit on the wire: a type and a text body. */
public final class Frame {
    private final FrameType type;
    private final String body;

    public Frame(FrameType type, String body) {
        this.type = Objects.requireNonNull(type);
        this.body = body == null ? "" : body;
    }

    public FrameType type() {
        return type;
    }

    public String body() {
        return body;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Frame)) return false;
        Frame f = (Frame) o;
        return type == f.type && body.equals(f.body);
    }

    @Override
    public int hashCode() {
        return Objects.hash(type, body);
    }

    @Override
    public String toString() {
        return type + "(" + body + ")";
    }
}
