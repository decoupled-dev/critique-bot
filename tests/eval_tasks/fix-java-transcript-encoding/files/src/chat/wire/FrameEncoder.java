package chat.wire;

import java.io.DataOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/** Writes frames as type, length, body. */
public final class FrameEncoder {
    public static final int MAX_BODY = 1 << 20;

    private final DataOutputStream out;

    public FrameEncoder(OutputStream out) {
        this.out = new DataOutputStream(out);
    }

    public void write(Frame frame) throws IOException {
        byte[] body = frame.body().getBytes(StandardCharsets.UTF_8);
        if (body.length > MAX_BODY) {
            throw new IOException("frame body too large: " + body.length);
        }
        out.writeByte(frame.type().code());
        out.writeInt(frame.body().length());
        out.write(body);
    }

    public void flush() throws IOException {
        out.flush();
    }
}
