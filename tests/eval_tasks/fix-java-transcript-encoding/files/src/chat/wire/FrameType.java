package chat.wire;

public enum FrameType {
    HEADER((byte) 1),
    MESSAGE((byte) 2),
    SYSTEM((byte) 3),
    END((byte) 9);

    private final byte code;

    FrameType(byte code) {
        this.code = code;
    }

    public byte code() {
        return code;
    }

    public static FrameType fromCode(int code) throws java.io.IOException {
        for (FrameType t : values()) {
            if (t.code == code) {
                return t;
            }
        }
        throw new java.io.IOException("unknown frame type " + code);
    }
}
