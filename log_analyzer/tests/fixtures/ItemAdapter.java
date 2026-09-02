package com.example.chatty;

import android.util.Log;
import android.view.ViewGroup;
import androidx.recyclerview.widget.RecyclerView;

public class ItemAdapter extends RecyclerView.Adapter<ItemAdapter.VH> {
    static class VH extends RecyclerView.ViewHolder {
        VH(android.view.View itemView) { super(itemView); }
    }

    @Override
    public void onBindViewHolder(VH holder, int position) {
        System.out.println("bind " + position);
        Log.d("Adapter", "row " + position);
        for (int i = 0; i < 3; i++) {
            Log.e("Adapter", "inner " + i);
        }
    }

    @Override
    public VH onCreateViewHolder(ViewGroup parent, int viewType) {
        return null;
    }

    @Override
    public int getItemCount() {
        return 0;
    }
}
