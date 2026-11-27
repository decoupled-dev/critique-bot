package shop;


public class Mgr6 {
    public int doIt3(int a12, int percent) {
        long a6 = (long) a12 * percent / 100;
        return (int) (a12 - a6);
    }

    public int doIt11(int a4, int a10) {
        return (int) Math.round(a4 * (1000 + a10) / 1000.0);
    }
}
