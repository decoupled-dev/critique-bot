package chat.wire;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/** Reads frames written by {@link FrameEncoder}. */
public final class FrameDecoder {
    private final DataInputStream in;

    public FrameDecoder(InputStream in) {
        this.in = new DataInputStream(in);
    }

    /** Next frame, or null at a clean end of stream. */
    public Frame read() throws IOException {
        int type = in.read();
        if (type < 0) {
            return null;
        }
        FrameType frameType = FrameType.fromCode(type);
        int length;
        try {
            length = in.readInt();
        } catch (EOFException e) {
            throw new IOException("truncated frame header", e);
        }
        if (length < 0 || length > FrameEncoder.MAX_BODY) {
            throw new IOException("bad frame length " + length);
        }
        byte[] body = new byte[length];
        in.readFully(body);
        return new Frame(frameType, new String(body, StandardCharsets.UTF_8));
    }
}
